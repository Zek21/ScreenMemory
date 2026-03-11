"""
Tool Synthesizer — Dynamically creates new tools/capabilities at runtime.
Claude/GPT have fixed tool sets. ScreenMemory generates its own tools on demand,
validates them for safety, caches them, and reuses them across sessions.
"""

import ast
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """Specification for a dynamically synthesized tool."""
    tool_id: str
    name: str
    description: str
    parameters: list[dict]  # [{name, type, required, description}]
    return_type: str
    source_code: str
    safety_score: float
    usage_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    version: int = 1
    category: str = "general"
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ToolSpec':
        return cls(**data)


class ToolValidator:
    """Validates generated tool code for syntax, safety, and execution."""
    
    DANGEROUS_MODULES = {
        'os.system', 'os.exec', 'os.spawn', 'os.popen',
        'subprocess.Popen', 'subprocess.call', 'subprocess.run',
        'eval', 'exec', '__import__', 'compile',
        'shutil.rmtree', 'shutil.move', 'shutil.copytree',
        'socket.socket', 'urllib.request.urlopen',
        'requests.get', 'requests.post',
    }
    
    DANGEROUS_PATTERNS = [
        r'open\s*\([^)]*["\']w["\']',  # File writes
        r'open\s*\([^)]*["\']a["\']',  # File appends
        r'__.*__',  # Dunder methods (potential abuse)
        r'globals\s*\(',  # Global scope access
        r'locals\s*\(',  # Local scope access
        r'setattr\s*\(',  # Dynamic attribute setting
        r'delattr\s*\(',  # Attribute deletion
        r'\.rm\s*\(',  # Removal operations
        r'\.unlink\s*\(',  # File deletion
    ]
    
    ALLOWED_IMPORTS = {
        'math', 'json', 're', 'datetime', 'collections',
        'itertools', 'hashlib', 'pathlib', 'typing',
        'dataclasses', 'functools', 'operator', 'statistics',
        'uuid', 'base64', 'textwrap', 'string', 'random',
    }
    
    def validate_syntax(self, code: str) -> tuple[bool, str]:
        """Check if code is syntactically valid Python."""
        try:
            ast.parse(code)
            return True, "Syntax valid"
        except SyntaxError as e:
            return False, f"Syntax error: {e}"
        except Exception as e:
            return False, f"Parse error: {e}"
    
    def validate_safety(self, code: str) -> tuple[bool, list[str]]:
        """Check for dangerous operations in code."""
        issues = []
        
        # Check for dangerous module usage
        for dangerous in self.DANGEROUS_MODULES:
            if dangerous in code:
                issues.append(f"Dangerous operation: {dangerous}")
        
        # Check for dangerous patterns
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, code):
                issues.append(f"Dangerous pattern: {pattern}")
        
        # Check imports using AST
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name not in self.ALLOWED_IMPORTS:
                            issues.append(f"Disallowed import: {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module not in self.ALLOWED_IMPORTS:
                        issues.append(f"Disallowed import from: {node.module}")
        except Exception as e:
            logger.debug("AST validation parse error: %s", e)
        
        return len(issues) == 0, issues
    
    def validate_execution(self, code: str, test_inputs: list[dict]) -> tuple[bool, str]:
        """Test execution of code in isolated subprocess."""
        try:
            # Create a test script
            test_script = f"""
import sys
import json

{code}

# Test execution
try:
    test_inputs = {test_inputs}
    results = []
    for test_input in test_inputs:
        result = tool_function(**test_input)
        results.append({{"success": True, "result": str(result)}})
    print(json.dumps({{"status": "success", "results": results}}))
except Exception as e:
    print(json.dumps({{"status": "error", "error": str(e)}}))
"""
            
            # Run in subprocess with timeout
            result = subprocess.run(
                ['python', '-c', test_script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode != 0:
                return False, f"Execution failed: {result.stderr}"
            
            try:
                output = json.loads(result.stdout)
                if output.get('status') == 'success':
                    return True, "Execution successful"
                else:
                    return False, f"Execution error: {output.get('error')}"
            except json.JSONDecodeError:
                return False, f"Invalid output: {result.stdout}"
                
        except subprocess.TimeoutExpired:
            return False, "Execution timeout (5s)"
        except Exception as e:
            return False, f"Validation error: {e}"
    
    def compute_safety_score(self, code: str) -> float:
        """Compute weighted safety score (0.0-1.0)."""
        score = 1.0
        
        # Syntax check (30% weight)
        syntax_valid, _ = self.validate_syntax(code)
        if not syntax_valid:
            score -= 0.3
        
        # Safety check (50% weight)
        safety_valid, issues = self.validate_safety(code)
        if not safety_valid:
            # Deduct based on number of issues
            deduction = min(0.5, len(issues) * 0.1)
            score -= deduction
        
        # Complexity check (20% weight)
        # Penalize overly complex code
        lines = len(code.split('\n'))
        if lines > 100:
            score -= 0.2
        elif lines > 50:
            score -= 0.1
        
        return max(0.0, score)


class ToolRegistry:
    """Persistent SQLite cache for synthesized tools."""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "tools.db"
        else:
            db_path = Path(db_path)
        
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS synthesized_tools (
                    tool_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    parameters TEXT,
                    return_type TEXT,
                    source_code TEXT NOT NULL,
                    safety_score REAL,
                    usage_count INTEGER DEFAULT 0,
                    created_at TEXT,
                    version INTEGER DEFAULT 1,
                    category TEXT DEFAULT 'general'
                )
            """)
            
            # Create indexes for search
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_name ON synthesized_tools(name)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_category ON synthesized_tools(category)
            """)
            
            # Full-text search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
                    tool_id, name, description, content=synthesized_tools
                )
            """)
            conn.commit()
    
    def save(self, spec: ToolSpec):
        """Save tool specification to database."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO synthesized_tools
                    (tool_id, name, description, parameters, return_type, source_code,
                     safety_score, usage_count, created_at, version, category)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    spec.tool_id,
                    spec.name,
                    spec.description,
                    json.dumps(spec.parameters),
                    spec.return_type,
                    spec.source_code,
                    spec.safety_score,
                    spec.usage_count,
                    spec.created_at,
                    spec.version,
                    spec.category
                ))
                
                # Update FTS index
                conn.execute("""
                    INSERT OR REPLACE INTO tools_fts(tool_id, name, description)
                    VALUES (?, ?, ?)
                """, (spec.tool_id, spec.name, spec.description))
                
                conn.commit()
    
    def load(self, tool_id: str) -> Optional[ToolSpec]:
        """Load tool by ID."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM synthesized_tools WHERE tool_id = ?",
                    (tool_id,)
                )
                row = cursor.fetchone()
                
                if row:
                    data = dict(row)
                    data['parameters'] = json.loads(data['parameters'])
                    return ToolSpec.from_dict(data)
                return None
    
    def load_by_name(self, name: str) -> Optional[ToolSpec]:
        """Load tool by name."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM synthesized_tools WHERE name = ? ORDER BY version DESC LIMIT 1",
                    (name,)
                )
                row = cursor.fetchone()
                
                if row:
                    data = dict(row)
                    data['parameters'] = json.loads(data['parameters'])
                    return ToolSpec.from_dict(data)
                return None
    
    def search(self, query: str) -> list[ToolSpec]:
        """Full-text search for tools."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT st.* FROM synthesized_tools st
                    JOIN tools_fts fts ON st.tool_id = fts.tool_id
                    WHERE tools_fts MATCH ?
                    ORDER BY st.usage_count DESC
                """, (query,))
                
                results = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data['parameters'] = json.loads(data['parameters'])
                    results.append(ToolSpec.from_dict(data))
                return results
    
    def list_all(self, category: str = None) -> list[ToolSpec]:
        """List all tools, optionally filtered by category."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if category:
                    cursor = conn.execute(
                        "SELECT * FROM synthesized_tools WHERE category = ? ORDER BY usage_count DESC",
                        (category,)
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM synthesized_tools ORDER BY usage_count DESC"
                    )
                
                results = []
                for row in cursor.fetchall():
                    data = dict(row)
                    data['parameters'] = json.loads(data['parameters'])
                    results.append(ToolSpec.from_dict(data))
                return results
    
    def increment_usage(self, tool_id: str):
        """Increment usage counter."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE synthesized_tools SET usage_count = usage_count + 1 WHERE tool_id = ?",
                    (tool_id,)
                )
                conn.commit()
    
    def delete(self, tool_id: str):
        """Delete tool from registry."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM synthesized_tools WHERE tool_id = ?", (tool_id,))
                conn.execute("DELETE FROM tools_fts WHERE tool_id = ?", (tool_id,))
                conn.commit()


class ToolSynthesizer:
    """Generates new tools from natural language descriptions."""
    
    def __init__(self, ollama_url: str = "http://localhost:11434", model: str = "qwen2.5-coder:3b"):
        self.ollama_url = ollama_url
        self.model = model
        self.validator = ToolValidator()
        self.registry = ToolRegistry()
    
    def synthesize(
        self,
        description: str,
        examples: list[dict] = None,
        category: str = "general"
    ) -> Optional[ToolSpec]:
        """Generate a tool from natural language description."""
        
        # Check if tool already exists
        existing = self.registry.search(description)
        if existing and existing[0].safety_score > 0.7:
            print(f"Found existing tool: {existing[0].name}")
            return existing[0]
        
        # Generate code with retries
        max_retries = 3
        for attempt in range(max_retries):
            try:
                code = self._generate_code(description, examples, attempt)
                if not code:
                    continue
                
                # Validate
                syntax_ok, syntax_msg = self.validator.validate_syntax(code)
                if not syntax_ok:
                    print(f"Attempt {attempt + 1}: Syntax error - {syntax_msg}")
                    continue
                
                safety_ok, safety_issues = self.validator.validate_safety(code)
                if not safety_ok:
                    print(f"Attempt {attempt + 1}: Safety issues - {safety_issues}")
                    continue
                
                # Extract metadata from code
                metadata = self._extract_metadata(code, description)
                
                # Test execution if examples provided
                if examples:
                    exec_ok, exec_msg = self.validator.validate_execution(code, examples)
                    if not exec_ok:
                        print(f"Attempt {attempt + 1}: Execution error - {exec_msg}")
                        continue
                
                # Compute safety score
                safety_score = self.validator.compute_safety_score(code)
                
                if safety_score < 0.6:
                    print(f"Attempt {attempt + 1}: Low safety score {safety_score}")
                    continue
                
                # Create tool spec
                spec = ToolSpec(
                    tool_id=str(uuid4()),
                    name=metadata['name'],
                    description=metadata['description'],
                    parameters=metadata['parameters'],
                    return_type=metadata['return_type'],
                    source_code=code,
                    safety_score=safety_score,
                    category=category
                )
                
                # Save to registry
                self.registry.save(spec)
                print(f"✓ Tool synthesized: {spec.name} (safety: {safety_score:.2f})")
                return spec
                
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                continue
        
        print(f"Failed to synthesize tool after {max_retries} attempts")
        return None
    
    def _generate_code(self, description: str, examples: list[dict], attempt: int) -> Optional[str]:
        """Generate Python code using Ollama."""
        
        examples_str = ""
        if examples:
            examples_str = "\n\nExample usage:\n"
            for i, ex in enumerate(examples, 1):
                examples_str += f"Example {i}: {ex}\n"
        
        prompt = f"""Generate a Python function that implements the following:

Description: {description}{examples_str}

Requirements:
1. Function MUST be named 'tool_function'
2. Include complete docstring with parameter descriptions
3. Use type hints
4. Only use allowed imports: math, json, re, datetime, collections, itertools, hashlib, pathlib, typing, functools, operator, statistics
5. NO file writes, NO network calls, NO dangerous operations
6. Handle errors gracefully with try/except
7. Return meaningful results

Generate ONLY the Python function code, no explanations:"""

        try:
            # Call Ollama API
            data = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3 + (attempt * 0.1),  # Increase randomness on retries
                    "top_p": 0.9,
                    "num_predict": 512
                }
            }
            
            req = urllib.request.Request(
                f"{self.ollama_url}/api/generate",
                data=json.dumps(data).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                code = result.get('response', '')
                
                # Extract code from markdown if present
                code = self._extract_code_from_response(code)
                return code
                
        except Exception as e:
            print(f"Code generation error: {e}")
            return None
    
    def _extract_code_from_response(self, response: str) -> str:
        """Extract Python code from LLM response."""
        # Remove markdown code blocks
        code = re.sub(r'```python\s*', '', response)
        code = re.sub(r'```\s*$', '', code)
        code = code.strip()
        
        # Find function definition
        lines = code.split('\n')
        function_lines = []
        in_function = False
        
        for line in lines:
            if line.strip().startswith('def tool_function'):
                in_function = True
            if in_function:
                function_lines.append(line)
        
        return '\n'.join(function_lines) if function_lines else code
    
    def _extract_metadata(self, code: str, description: str) -> dict:
        """Extract metadata from generated code."""
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == 'tool_function':
                    # Extract parameters
                    params = []
                    for arg in node.args.args:
                        param_type = "Any"
                        if arg.annotation:
                            param_type = ast.unparse(arg.annotation)
                        
                        params.append({
                            'name': arg.arg,
                            'type': param_type,
                            'required': True,
                            'description': f"Parameter {arg.arg}"
                        })
                    
                    # Extract return type
                    return_type = "Any"
                    if node.returns:
                        return_type = ast.unparse(node.returns)
                    
                    # Extract docstring
                    docstring = ast.get_docstring(node) or description
                    
                    # Generate name from description
                    name = self._generate_name(description)
                    
                    return {
                        'name': name,
                        'description': docstring[:200],
                        'parameters': params,
                        'return_type': return_type
                    }
        except Exception as e:
            logger.debug("Code spec extraction failed: %s", e)
        
        # Fallback
        return {
            'name': self._generate_name(description),
            'description': description,
            'parameters': [],
            'return_type': 'Any'
        }
    
    def _generate_name(self, description: str) -> str:
        """Generate function name from description."""
        # Take first few words, convert to snake_case
        words = re.findall(r'\w+', description.lower())[:4]
        return '_'.join(words)
    
    def get_tool(self, name: str) -> Optional[ToolSpec]:
        """Retrieve cached tool by name."""
        return self.registry.load_by_name(name)
    
    def execute_tool(self, tool_id: str, **kwargs) -> Any:
        """Execute a synthesized tool safely in subprocess."""
        spec = self.registry.load(tool_id)
        if not spec:
            raise ValueError(f"Tool {tool_id} not found")
        
        if spec.safety_score < 0.6:
            raise ValueError(f"Tool safety score too low: {spec.safety_score}")
        
        # Create execution script
        exec_script = f"""
import json
import sys

{spec.source_code}

try:
    kwargs = {repr(kwargs)}
    result = tool_function(**kwargs)
    print(json.dumps({{"status": "success", "result": result}}, default=str))
except Exception as e:
    print(json.dumps({{"status": "error", "error": str(e)}}))
"""
        
        try:
            result = subprocess.run(
                ['python', '-c', exec_script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                raise RuntimeError(f"Execution failed: {result.stderr}")
            
            output = json.loads(result.stdout)
            if output['status'] == 'success':
                self.registry.increment_usage(tool_id)
                return output['result']
            else:
                raise RuntimeError(output['error'])
                
        except subprocess.TimeoutExpired:
            raise TimeoutError("Tool execution timeout")
        except Exception as e:
            raise RuntimeError(f"Execution error: {e}")
    
    def list_tools(self, category: str = None) -> list[ToolSpec]:
        """List available synthesized tools."""
        return self.registry.list_all(category)
    
    def retire_tool(self, tool_id: str):
        """Mark tool as deprecated (delete from registry)."""
        self.registry.delete(tool_id)


class ToolComposer:
    """Compose multiple tools into pipelines."""
    
    def __init__(self, synthesizer: ToolSynthesizer):
        self.synthesizer = synthesizer
    
    def compose(self, tool_ids: list[str], description: str) -> Optional[ToolSpec]:
        """Chain multiple tools into a pipeline."""
        # Load all tools
        tools = []
        for tid in tool_ids:
            spec = self.synthesizer.registry.load(tid)
            if not spec:
                print(f"Tool {tid} not found")
                return None
            tools.append(spec)
        
        # Generate pipeline code
        pipeline_code = self._generate_pipeline_code(tools, description)
        
        # Validate pipeline
        syntax_ok, _ = self.synthesizer.validator.validate_syntax(pipeline_code)
        safety_ok, _ = self.synthesizer.validator.validate_safety(pipeline_code)
        
        if not syntax_ok or not safety_ok:
            print("Pipeline validation failed")
            return None
        
        # Create composite tool spec
        safety_score = min(tool.safety_score for tool in tools)
        
        spec = ToolSpec(
            tool_id=str(uuid4()),
            name=f"pipeline_{'_'.join(t.name[:10] for t in tools[:3])}",
            description=description,
            parameters=tools[0].parameters,  # Use first tool's params
            return_type=tools[-1].return_type,  # Use last tool's return
            source_code=pipeline_code,
            safety_score=safety_score,
            category="pipeline"
        )
        
        self.synthesizer.registry.save(spec)
        return spec
    
    def _generate_pipeline_code(self, tools: list[ToolSpec], description: str) -> str:
        """Generate code that chains tools together."""
        code_parts = [f"# Pipeline: {description}\n"]
        
        # Include all tool functions
        for i, tool in enumerate(tools):
            renamed = tool.source_code.replace('def tool_function', f'def tool_step_{i}')
            code_parts.append(f"\n# Step {i+1}: {tool.name}\n{renamed}\n")
        
        # Generate main pipeline function
        code_parts.append("\ndef tool_function(**kwargs):\n")
        code_parts.append("    \"\"\"Pipeline function that chains multiple tools.\"\"\"\n")
        code_parts.append("    result = kwargs\n")
        
        for i in range(len(tools)):
            code_parts.append(f"    result = tool_step_{i}(**result)\n")
        
        code_parts.append("    return result\n")
        
        return ''.join(code_parts)
    
    def decompose(self, complex_description: str) -> list[str]:
        """Break complex tool request into simpler sub-tools."""
        # Use LLM to decompose (simplified version)
        # In production, this would call Ollama to analyze and break down
        
        # For now, simple heuristic: split by "and", "then", "after"
        delimiters = [' and ', ' then ', ' after ', ', ']
        parts = [complex_description]
        
        for delim in delimiters:
            new_parts = []
            for part in parts:
                new_parts.extend(part.split(delim))
            parts = new_parts
        
        # Clean and return
        return [p.strip() for p in parts if p.strip()]


# Example usage
if __name__ == "__main__":
    # Initialize synthesizer
    synth = ToolSynthesizer()
    
    # Synthesize a simple tool
    tool = synth.synthesize(
        description="Calculate the factorial of a number",
        examples=[
            {"n": 5},
            {"n": 3}
        ],
        category="math"
    )
    
    if tool:
        print(f"\nTool ID: {tool.tool_id}")
        print(f"Name: {tool.name}")
        print(f"Description: {tool.description}")
        print(f"Safety Score: {tool.safety_score}")
        print(f"\nCode:\n{tool.source_code}")
        
        # Execute the tool
        try:
            result = synth.execute_tool(tool.tool_id, n=5)
            print(f"\nExecution result: {result}")
        except Exception as e:
            print(f"Execution error: {e}")
    
    # List all tools
    print("\n\nAll synthesized tools:")
    for t in synth.list_tools():
        print(f"  - {t.name} (usage: {t.usage_count}, safety: {t.safety_score:.2f})")
