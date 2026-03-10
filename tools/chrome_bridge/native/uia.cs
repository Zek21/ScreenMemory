// uia.exe — Windows UI Automation Scanner
// Compiled with: csc.exe /target:exe /r:UIAutomationClient.dll /r:UIAutomationTypes.dll /r:WindowsBase.dll uia.cs
// Sees EVERY accessible element on screen: buttons, toggles, text, links, trees, tabs
// Can invoke, toggle, focus, and click elements by name — no pixel guessing

using System;
using System.Text;
using System.Windows;
using System.Windows.Automation;
using System.Globalization;
using System.Collections.Generic;
using System.Runtime.InteropServices;

class UIA
{
    static int maxDepth = 4;

    static void Main(string[] args)
    {
        if (args.Length == 0) { Usage(); return; }
        try
        {
            switch (args[0].ToLower())
            {
                case "scan":    CmdScan(args); break;
                case "find":    CmdFind(args); break;
                case "invoke":  CmdInvoke(args); break;
                case "at":      CmdAt(args); break;
                case "tree":    CmdTree(args); break;
                case "focus":   CmdFocus(args); break;
                case "value":   CmdValue(args); break;
                case "walk":    CmdWalk(args); break;
                default:        Usage(); break;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Error: " + ex.Message);
            Environment.Exit(2);
        }
    }

    static void Usage()
    {
        Console.Error.WriteLine(@"uia — Windows UI Automation Scanner

Commands:
  scan [hwnd] [--depth N]       Element tree as JSON (default depth 4)
  find <name> [hwnd] [--depth N] Find elements by name substring
  invoke <name> [hwnd]          Find and invoke/toggle/click element
  at <x> <y>                    Element at screen coordinates
  tree [hwnd] [--depth N]       Human-readable element tree
  focus <name> [hwnd]           Find and focus element
  value <name> [newval] [hwnd]  Get or set element value
  walk <hwnd> [--depth N]       Deep-walk single window");
    }

    // --- Parse args for hwnd and --depth ---
    static AutomationElement GetRoot(string[] args, int startIdx)
    {
        for (int i = startIdx; i < args.Length; i++)
        {
            if (args[i] == "--depth" && i + 1 < args.Length)
            {
                maxDepth = int.Parse(args[++i]);
                continue;
            }
            long hwnd;
            if (long.TryParse(args[i], out hwnd) && hwnd != 0)
            {
                try { return AutomationElement.FromHandle(new IntPtr(hwnd)); }
                catch { Console.Error.WriteLine("Warning: hwnd " + hwnd + " not found, using root"); }
            }
        }
        return AutomationElement.RootElement;
    }

    // ========== SCAN ==========
    static void CmdScan(string[] args)
    {
        var root = GetRoot(args, 1);
        var sb = new StringBuilder(65536);
        sb.Append('[');
        bool first = true;

        AutomationElementCollection children;
        try { children = root.FindAll(TreeScope.Children, Condition.TrueCondition); }
        catch { Console.Write("[]"); return; }

        foreach (AutomationElement child in children)
        {
            try
            {
                // Skip invisible/offscreen windows
                var rect = child.Current.BoundingRectangle;
                if (rect.IsEmpty || double.IsInfinity(rect.Width)) continue;
                if (rect.Width < 1 || rect.Height < 1) continue;

                if (!first) sb.Append(',');
                first = false;
                EmitJson(child, sb, 0);
            }
            catch { }
        }
        sb.Append(']');
        Console.Write(sb.ToString());
    }

    // ========== FIND ==========
    static void CmdFind(string[] args)
    {
        if (args.Length < 2) { Console.Error.WriteLine("Usage: uia find <name> [hwnd]"); Environment.Exit(1); }
        string search = args[1].ToLower();
        var root = GetRoot(args, 2);

        var sb = new StringBuilder(8192);
        sb.Append('[');
        bool first = true;
        SearchByName(root, search, sb, ref first, 0, Math.Max(maxDepth, 8));
        sb.Append(']');
        Console.Write(sb.ToString());
    }

    static void SearchByName(AutomationElement el, string search, StringBuilder sb, ref bool first, int depth, int maxD)
    {
        if (depth > maxD) return;
        AutomationElementCollection children;
        try { children = el.FindAll(TreeScope.Children, Condition.TrueCondition); }
        catch { return; }

        foreach (AutomationElement child in children)
        {
            try
            {
                string name = (child.Current.Name ?? "").ToLower();
                string autoId = (child.Current.AutomationId ?? "").ToLower();
                string cls = (child.Current.ClassName ?? "").ToLower();
                if (name.Contains(search) || autoId.Contains(search) || cls.Contains(search))
                {
                    if (!first) sb.Append(',');
                    first = false;
                    EmitJson(child, sb, 0); // flat result, no children
                }
                SearchByName(child, search, sb, ref first, depth + 1, maxD);
            }
            catch { }
        }
    }

    // ========== INVOKE ==========
    static void CmdInvoke(string[] args)
    {
        if (args.Length < 2) { Console.Error.WriteLine("Usage: uia invoke <name> [hwnd]"); Environment.Exit(1); }
        string search = args[1].ToLower();
        var root = GetRoot(args, 2);

        var found = FindFirst(root, search, 0, 10);
        if (found == null)
        {
            Console.Error.WriteLine("NOT FOUND: " + args[1]);
            Environment.Exit(1);
        }

        var cur = found.Current;
        Console.Error.WriteLine("Target: [" + TypeName(cur.ControlType) + "] " + cur.Name);

        // Strategy 1: InvokePattern (buttons, menu items, links)
        try
        {
            var ip = (InvokePattern)found.GetCurrentPattern(InvokePattern.Pattern);
            ip.Invoke();
            Emit("{\"ok\":true,\"method\":\"invoke\",\"name\":" + Esc(cur.Name) + "}");
            return;
        }
        catch { }

        // Strategy 2: TogglePattern (checkboxes, toggle switches)
        try
        {
            var tp = (TogglePattern)found.GetCurrentPattern(TogglePattern.Pattern);
            string before = tp.Current.ToggleState.ToString();
            tp.Toggle();
            string after = tp.Current.ToggleState.ToString();
            Emit("{\"ok\":true,\"method\":\"toggle\",\"before\":\"" + before + "\",\"after\":\"" + after + "\",\"name\":" + Esc(cur.Name) + "}");
            return;
        }
        catch { }

        // Strategy 3: SelectionItemPattern
        try
        {
            var sp = (SelectionItemPattern)found.GetCurrentPattern(SelectionItemPattern.Pattern);
            sp.Select();
            Emit("{\"ok\":true,\"method\":\"select\",\"name\":" + Esc(cur.Name) + "}");
            return;
        }
        catch { }

        // Strategy 4: ExpandCollapsePattern
        try
        {
            var ep = (ExpandCollapsePattern)found.GetCurrentPattern(ExpandCollapsePattern.Pattern);
            if (ep.Current.ExpandCollapseState == ExpandCollapseState.Collapsed)
                ep.Expand();
            else
                ep.Collapse();
            Emit("{\"ok\":true,\"method\":\"expand\",\"name\":" + Esc(cur.Name) + "}");
            return;
        }
        catch { }

        // Strategy 5: Click at center via SetCursorPos + mouse_event
        var rect = cur.BoundingRectangle;
        if (!rect.IsEmpty && !double.IsInfinity(rect.X) && rect.Width > 0)
        {
            int cx = (int)(rect.X + rect.Width / 2);
            int cy = (int)(rect.Y + rect.Height / 2);
            SetCursorPos(cx, cy);
            System.Threading.Thread.Sleep(30);
            mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, IntPtr.Zero);
            System.Threading.Thread.Sleep(30);
            mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, IntPtr.Zero);
            Emit("{\"ok\":true,\"method\":\"click\",\"x\":" + cx + ",\"y\":" + cy + ",\"name\":" + Esc(cur.Name) + "}");
            return;
        }

        // Strategy 6: Try SetFocus
        try
        {
            found.SetFocus();
            Emit("{\"ok\":true,\"method\":\"focus\",\"name\":" + Esc(cur.Name) + "}");
            return;
        }
        catch { }

        Console.Error.WriteLine("FAILED: no invoke strategy worked for " + cur.Name);
        Environment.Exit(1);
    }

    // ========== AT ==========
    static void CmdAt(string[] args)
    {
        if (args.Length < 3) { Console.Error.WriteLine("Usage: uia at <x> <y>"); Environment.Exit(1); }
        int x = int.Parse(args[1]);
        int y = int.Parse(args[2]);

        try
        {
            var el = AutomationElement.FromPoint(new Point(x, y));
            if (el != null)
            {
                var sb = new StringBuilder();
                EmitJson(el, sb, 0);
                Console.Write(sb.ToString());
                return;
            }
        }
        catch { }
        Console.Write("null");
    }

    // ========== TREE ==========
    static void CmdTree(string[] args)
    {
        var root = GetRoot(args, 1);
        PrintTree(root, "", 0);
    }

    static void PrintTree(AutomationElement el, string indent, int depth)
    {
        if (depth > maxDepth) return;
        var cur = el.Current;
        var rect = cur.BoundingRectangle;

        string pos = "";
        if (!rect.IsEmpty && !double.IsInfinity(rect.X))
            pos = String.Format(" @{0},{1} {2}x{3}", (int)rect.X, (int)rect.Y, (int)rect.Width, (int)rect.Height);

        string type = TypeName(cur.ControlType);
        string name = cur.Name ?? "";
        if (name.Length > 80) name = name.Substring(0, 77) + "...";

        // Show patterns
        var pats = new List<string>();
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsInvokePatternAvailableProperty)) pats.Add("INV"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsTogglePatternAvailableProperty)) pats.Add("TOG"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsValuePatternAvailableProperty)) pats.Add("VAL"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsExpandCollapsePatternAvailableProperty)) pats.Add("EXP"); } catch { }
        string patStr = pats.Count > 0 ? " [" + string.Join(",", pats) + "]" : "";

        Console.WriteLine("{0}{1} \"{2}\"{3}{4}", indent, type, name, pos, patStr);

        try
        {
            var children = el.FindAll(TreeScope.Children, Condition.TrueCondition);
            foreach (AutomationElement child in children)
            {
                try { PrintTree(child, indent + "  ", depth + 1); }
                catch { }
            }
        }
        catch { }
    }

    // ========== FOCUS ==========
    static void CmdFocus(string[] args)
    {
        if (args.Length < 2) { Console.Error.WriteLine("Usage: uia focus <name> [hwnd]"); Environment.Exit(1); }
        var root = GetRoot(args, 2);
        var found = FindFirst(root, args[1].ToLower(), 0, 10);
        if (found == null) { Console.Error.WriteLine("NOT FOUND"); Environment.Exit(1); }
        try { found.SetFocus(); } catch { }
        Emit("{\"ok\":true,\"name\":" + Esc(found.Current.Name) + "}");
    }

    // ========== VALUE ==========
    static void CmdValue(string[] args)
    {
        if (args.Length < 2) { Console.Error.WriteLine("Usage: uia value <name> [newval] [hwnd]"); Environment.Exit(1); }
        string search = args[1].ToLower();

        // Determine if setting value
        string newVal = null;
        var parseArgs = new List<string>();
        long dummy;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i].StartsWith("--") || long.TryParse(args[i], out dummy))
                parseArgs.Add(args[i]);
            else if (newVal == null)
                newVal = args[i];
            else
                parseArgs.Add(args[i]);
        }

        var root = GetRoot(parseArgs.ToArray(), 0);
        var found = FindFirst(root, search, 0, 10);
        if (found == null) { Console.Error.WriteLine("NOT FOUND"); Environment.Exit(1); }

        try
        {
            var vp = (ValuePattern)found.GetCurrentPattern(ValuePattern.Pattern);
            if (newVal != null)
            {
                vp.SetValue(newVal);
                Emit("{\"ok\":true,\"name\":" + Esc(found.Current.Name) + ",\"value\":" + Esc(newVal) + "}");
            }
            else
            {
                Emit("{\"name\":" + Esc(found.Current.Name) + ",\"value\":" + Esc(vp.Current.Value) + "}");
            }
        }
        catch
        {
            Console.Error.WriteLine("Element does not support ValuePattern");
            Environment.Exit(1);
        }
    }

    // ========== WALK (deep single window) ==========
    static void CmdWalk(string[] args)
    {
        if (args.Length < 2) { Console.Error.WriteLine("Usage: uia walk <hwnd> [--depth N]"); Environment.Exit(1); }
        long hwnd;
        if (!long.TryParse(args[1], out hwnd)) { Console.Error.WriteLine("Invalid hwnd"); Environment.Exit(1); }

        for (int i = 2; i < args.Length; i++)
            if (args[i] == "--depth" && i + 1 < args.Length)
                maxDepth = int.Parse(args[++i]);

        AutomationElement el;
        try { el = AutomationElement.FromHandle(new IntPtr(hwnd)); }
        catch { Console.Error.WriteLine("Window not found"); Environment.Exit(1); return; }

        var sb = new StringBuilder(65536);
        EmitJson(el, sb, 0);
        Console.Write(sb.ToString());
    }

    // ========== Helpers ==========

    static AutomationElement FindFirst(AutomationElement el, string search, int depth, int maxD)
    {
        if (depth > maxD) return null;
        AutomationElementCollection children;
        try { children = el.FindAll(TreeScope.Children, Condition.TrueCondition); }
        catch { return null; }

        // Breadth-first: check all children first
        foreach (AutomationElement child in children)
        {
            try
            {
                string name = (child.Current.Name ?? "").ToLower();
                string autoId = (child.Current.AutomationId ?? "").ToLower();
                if (name.Contains(search) || autoId.Contains(search))
                    return child;
            }
            catch { }
        }
        // Then recurse
        foreach (AutomationElement child in children)
        {
            try
            {
                var found = FindFirst(child, search, depth + 1, maxD);
                if (found != null) return found;
            }
            catch { }
        }
        return null;
    }

    static void EmitJson(AutomationElement el, StringBuilder sb, int depth)
    {
        var cur = el.Current;
        var rect = cur.BoundingRectangle;

        sb.Append('{');
        sb.Append("\"name\":").Append(Esc(cur.Name ?? ""));
        sb.Append(",\"type\":\"").Append(TypeName(cur.ControlType)).Append('"');
        sb.Append(",\"autoId\":").Append(Esc(cur.AutomationId ?? ""));
        sb.Append(",\"class\":").Append(Esc(cur.ClassName ?? ""));

        int h = cur.NativeWindowHandle;
        if (h != 0) sb.Append(",\"hwnd\":").Append(h);

        if (!rect.IsEmpty && !double.IsInfinity(rect.X))
        {
            sb.AppendFormat(CultureInfo.InvariantCulture,
                ",\"x\":{0},\"y\":{1},\"w\":{2},\"h\":{3}",
                (int)rect.X, (int)rect.Y, (int)rect.Width, (int)rect.Height);
        }

        // Patterns
        var pats = new List<string>();
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsInvokePatternAvailableProperty)) pats.Add("invoke"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsTogglePatternAvailableProperty)) pats.Add("toggle"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsValuePatternAvailableProperty)) pats.Add("value"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsExpandCollapsePatternAvailableProperty)) pats.Add("expand"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsSelectionItemPatternAvailableProperty)) pats.Add("select"); } catch { }
        try { if ((bool)el.GetCurrentPropertyValue(AutomationElement.IsScrollPatternAvailableProperty)) pats.Add("scroll"); } catch { }

        if (pats.Count > 0)
        {
            sb.Append(",\"patterns\":[");
            for (int i = 0; i < pats.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('"').Append(pats[i]).Append('"');
            }
            sb.Append(']');
        }

        // Value
        if (pats.Contains("value"))
        {
            try
            {
                var vp = (ValuePattern)el.GetCurrentPattern(ValuePattern.Pattern);
                sb.Append(",\"value\":").Append(Esc(vp.Current.Value ?? ""));
            }
            catch { }
        }

        // Toggle state
        if (pats.Contains("toggle"))
        {
            try
            {
                var tp = (TogglePattern)el.GetCurrentPattern(TogglePattern.Pattern);
                sb.Append(",\"toggleState\":\"").Append(tp.Current.ToggleState).Append('"');
            }
            catch { }
        }

        // Children
        if (depth < maxDepth)
        {
            try
            {
                var children = el.FindAll(TreeScope.Children, Condition.TrueCondition);
                if (children.Count > 0)
                {
                    sb.Append(",\"children\":[");
                    bool first = true;
                    foreach (AutomationElement child in children)
                    {
                        try
                        {
                            var cr = child.Current.BoundingRectangle;
                            if (cr.IsEmpty || double.IsInfinity(cr.Width)) continue;

                            if (!first) sb.Append(',');
                            first = false;
                            EmitJson(child, sb, depth + 1);
                        }
                        catch { }
                    }
                    sb.Append(']');
                }
            }
            catch { }
        }

        sb.Append('}');
    }

    static string TypeName(ControlType ct)
    {
        return ct.ProgrammaticName.Replace("ControlType.", "");
    }

    static string Esc(string s)
    {
        if (s == null) return "\"\"";
        var sb = new StringBuilder(s.Length + 2);
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"':  sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.AppendFormat("\\u{0:x4}", (int)c);
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    static void Emit(string json) { Console.Write(json); }

    // P/Invoke for fallback clicking
    [DllImport("user32.dll")] static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr ei);
    const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    const uint MOUSEEVENTF_LEFTUP = 0x0004;
}
