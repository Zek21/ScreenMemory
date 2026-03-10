package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// Element represents a unified UI element from any source (UIA, CDP, Win32)
type Element struct {
	Source     string    `json:"source"`               // "uia", "cdp", "win32"
	Name      string    `json:"name"`
	Type      string    `json:"type"`                  // Button, Edit, Pane, etc.
	AutoID    string    `json:"autoId,omitempty"`
	Class     string    `json:"class,omitempty"`
	HWND      int       `json:"hwnd,omitempty"`
	X         int       `json:"x,omitempty"`
	Y         int       `json:"y,omitempty"`
	W         int       `json:"w,omitempty"`
	H         int       `json:"h,omitempty"`
	Z         int       `json:"z,omitempty"`           // z-order (0 = topmost)
	Patterns  []string  `json:"patterns,omitempty"`    // invoke, toggle, value, etc.
	Value     string    `json:"value,omitempty"`
	ToggleState string  `json:"toggleState,omitempty"`
	Children  []Element `json:"children,omitempty"`
}

// --- UIA Integration ---

func uiaExePath() string {
	// Look next to our own exe first, then in dist/, then in native/
	self, _ := os.Executable()
	dir := filepath.Dir(self)
	candidates := []string{
		filepath.Join(dir, "uia.exe"),
		filepath.Join(dir, "..", "dist", "uia.exe"),
		filepath.Join(dir, "..", "native", "uia.exe"),
		"uia.exe",
	}
	for _, c := range candidates {
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	return "uia.exe" // hope it's in PATH
}

func runUIA(args ...string) ([]byte, error) {
	exe := uiaExePath()
	cmd := exec.Command(exe, args...)
	cmd.Stderr = os.Stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("uia %s: %w", strings.Join(args, " "), err)
	}
	return out, nil
}

// ScanUIA scans a window (or desktop) via UIA and returns elements
func ScanUIA(hwnd uintptr, depth int) ([]Element, error) {
	args := []string{"scan"}
	if hwnd != 0 {
		args = append(args, strconv.FormatUint(uint64(hwnd), 10))
	}
	args = append(args, "--depth", strconv.Itoa(depth))

	out, err := runUIA(args...)
	if err != nil {
		return nil, err
	}

	var elements []Element
	if err := json.Unmarshal(out, &elements); err != nil {
		return nil, fmt.Errorf("parse UIA JSON: %w (got %d bytes)", err, len(out))
	}

	// Tag source
	tagSource(elements, "uia")
	return elements, nil
}

// FindUIA finds elements by name via UIA
func FindUIA(name string, hwnd uintptr) ([]Element, error) {
	args := []string{"find", name}
	if hwnd != 0 {
		args = append(args, strconv.FormatUint(uint64(hwnd), 10))
	}

	out, err := runUIA(args...)
	if err != nil {
		return nil, err
	}

	var elements []Element
	if err := json.Unmarshal(out, &elements); err != nil {
		return nil, fmt.Errorf("parse UIA find JSON: %w", err)
	}
	tagSource(elements, "uia")
	return elements, nil
}

// InvokeUIA finds and invokes an element by name
func InvokeUIA(name string, hwnd uintptr) (string, error) {
	args := []string{"invoke", name}
	if hwnd != 0 {
		args = append(args, strconv.FormatUint(uint64(hwnd), 10))
	}

	out, err := runUIA(args...)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// UIAElementAt returns what's at screen coordinates
func UIAElementAt(x, y int) (*Element, error) {
	out, err := runUIA("at", strconv.Itoa(x), strconv.Itoa(y))
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(string(out)) == "null" {
		return nil, fmt.Errorf("nothing at %d,%d", x, y)
	}

	var el Element
	if err := json.Unmarshal(out, &el); err != nil {
		return nil, err
	}
	el.Source = "uia"
	return &el, nil
}

// UIATree prints human-readable tree
func UIATree(hwnd uintptr, depth int) error {
	args := []string{"tree"}
	if hwnd != 0 {
		args = append(args, strconv.FormatUint(uint64(hwnd), 10))
	}
	args = append(args, "--depth", strconv.Itoa(depth))

	cmd := exec.Command(uiaExePath(), args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// --- Win32 Spatial ---

// GetZOrder returns visible windows in z-order (front to back)
func GetZOrder() []WindowInfo {
	getWindow := user32.NewProc("GetWindow")

	var ordered []WindowInfo
	topHwnd, _, _ := getWindow.Call(
		uintptr(getForegroundWindowCall()),
		0, // GW_HWNDFIRST — not right, let me use EnumWindows order which IS z-order
	)
	_ = topHwnd

	// EnumWindows already returns in z-order (top to bottom)
	windows := EnumAllWindows(true)
	for i, w := range windows {
		w.HWND = windows[i].HWND
		ordered = append(ordered, w)
	}
	return ordered
}

func getForegroundWindowCall() uintptr {
	hwnd, _, _ := getForegroundWindow.Call()
	return hwnd
}

// EnumChildWindowsOf lists child windows of a parent
func EnumChildWindowsOf(parent uintptr) []WindowInfo {
	enumChildWindows := user32.NewProc("EnumChildWindows")
	var children []WindowInfo

	cb := syscall.NewCallback(func(hwnd uintptr, _ uintptr) uintptr {
		title := getWindowTitle(hwnd)
		cls := getWindowClass(hwnd)
		var pid uint32
		getWindowThreadProcessId.Call(hwnd, uintptr(unsafe.Pointer(&pid)))
		children = append(children, WindowInfo{HWND: hwnd, Title: title, Class: cls, PID: pid})
		return 1
	})
	enumChildWindows.Call(parent, cb, 0)
	return children
}

// Ensure imports are used
var _ = syscall.NewCallback
var _ = unsafe.Pointer(nil)

// --- Full Scan (combined) ---

// FullScan merges Win32 window list + UIA tree
func FullScan(hwnd uintptr, depth int) ([]Element, error) {
	start := time.Now()

	// Parallel: get Win32 windows + UIA scan
	var uiaElements []Element
	var uiaErr error

	// UIA scan
	uiaElements, uiaErr = ScanUIA(hwnd, depth)

	// Win32 windows (always available, fast)
	win32Windows := EnumAllWindows(true)
	var win32Elements []Element
	for i, w := range win32Windows {
		rect := GetWindowRectInfo(w.HWND)
		win32Elements = append(win32Elements, Element{
			Source: "win32",
			Name:   w.Title,
			Type:   "Window",
			Class:  w.Class,
			HWND:   int(w.HWND),
			X:      int(rect.Left),
			Y:      int(rect.Top),
			W:      int(rect.Right - rect.Left),
			H:      int(rect.Bottom - rect.Top),
			Z:      i, // EnumWindows returns in z-order
		})
	}

	// Merge
	var all []Element
	all = append(all, win32Elements...)
	if uiaErr == nil {
		all = append(all, uiaElements...)
	}

	elapsed := time.Since(start)
	fmt.Fprintf(os.Stderr, "Scan: %d win32 + %d uia elements in %s\n", len(win32Elements), len(uiaElements), elapsed)

	return all, uiaErr
}

// --- CDP DOM Vision ---

// ScanCDPElements gets clickable elements from Chrome via CDP
func ScanCDPElements(port int) ([]Element, error) {
	if port == 0 {
		port = FindChromeDebugPort()
	}
	if port == 0 {
		return nil, fmt.Errorf("no Chrome debug port found")
	}

	cdp := NewCDP("127.0.0.1", port)
	tabs, err := cdp.Tabs()
	if err != nil {
		return nil, err
	}
	if len(tabs) == 0 {
		return nil, fmt.Errorf("no tabs")
	}

	tab := tabs[0]
	if err := tab.Connect(); err != nil {
		return nil, err
	}
	defer tab.Close()

	// Get all interactive elements with their bounding rects
	js := `(() => {
		const els = document.querySelectorAll('a, button, input, select, textarea, [role=button], [role=link], [role=tab], [onclick], [tabindex]');
		const results = [];
		els.forEach(el => {
			const rect = el.getBoundingClientRect();
			if (rect.width < 1 || rect.height < 1) return;
			results.push({
				source: "cdp",
				name: el.textContent?.trim().substring(0, 100) || el.getAttribute('aria-label') || el.getAttribute('title') || '',
				type: el.tagName,
				class: el.className?.substring?.(0, 60) || '',
				autoId: el.id || '',
				x: Math.round(rect.x),
				y: Math.round(rect.y),
				w: Math.round(rect.width),
				h: Math.round(rect.height),
				patterns: el.tagName === 'INPUT' ? ['value'] : el.tagName === 'BUTTON' || el.tagName === 'A' ? ['invoke'] : []
			});
		});
		return JSON.stringify(results);
	})()`

	result, err := tab.Eval(js)
	if err != nil {
		return nil, err
	}

	// Parse the JSON string result
	var strResult string
	if err := json.Unmarshal(result, &strResult); err != nil {
		// Try direct parse
		var elements []Element
		if err2 := json.Unmarshal(result, &elements); err2 != nil {
			return nil, fmt.Errorf("parse CDP elements: %w", err)
		}
		return elements, nil
	}

	var elements []Element
	if err := json.Unmarshal([]byte(strResult), &elements); err != nil {
		return nil, fmt.Errorf("parse CDP elements inner: %w", err)
	}
	return elements, nil
}

// --- Helpers ---

func tagSource(elements []Element, source string) {
	for i := range elements {
		elements[i].Source = source
		tagSource(elements[i].Children, source)
	}
}


