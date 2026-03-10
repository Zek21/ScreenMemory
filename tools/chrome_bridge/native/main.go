package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

func usage() {
	fmt.Println(`winctl — Native Windows Desktop Controller + Vision (Go)

Usage: winctl <command> [args...]

Desktop Commands:
  windows                    List visible windows (z-order)
  children <hwnd>            List child windows
  screen                     Screen dimensions
  monitors                   Monitor info
  foreground                 Foreground window
  screenshot [path]          Screenshot (default: screenshot.png)
  screenshot -w <title>      Screenshot specific window
  screenshot -r x,y,w,h     Screenshot region
  focus <title>              Focus/activate window
  move <title> x y [w h]    Move/resize window
  maximize <title>           Maximize window
  minimize <title>           Minimize window
  close <title>              Close window
  click <x> <y>             Click at screen coordinates (virtual)
  nav <url>                 Navigate foreground Chrome to URL
  type <text>                Type text (virtual keyboard)
  key <name>                 Press key (enter, tab, f5, etc.)
  hotkey <k1+k2+...>         Key combo (ctrl+c, alt+f4, etc.)
  clip                       Get clipboard
  clip set <text>            Set clipboard

Vision Commands (UI Automation):
  scan [hwnd] [--depth N]    Spatial scan (UIA + Win32 + CDP)
  find <name> [hwnd]         Find elements by name (any source)
  invoke <name> [hwnd]       Find and click/activate element
  at <x> <y>                 Identify element at coordinates
  tree [hwnd] [--depth N]    Human-readable element tree
  uia <subcommand>           Direct UIA scanner access

CDP Commands:
  cdp tabs                   List Chrome tabs
  cdp eval <expr>            Evaluate JS in first tab
  cdp nav <url>              Navigate first tab
  cdp shot [path]            Screenshot first tab
  cdp click <x> <y>          Click in page
  cdp type <text>            Type in page
  cdp version                Chrome version

Server:
  serve [port]               Start HTTP API (default: 8421)

Automation:
  install-ext <profile> <path>  Install extension on Chrome profile
  sleep <ms>                    Sleep for N milliseconds

Options:
  --json                     Force JSON output
  --help                     Show this help`)
}

func main() {
	args := os.Args[1:]
	if len(args) == 0 {
		usage()
		os.Exit(0)
	}

	cmd := args[0]
	rest := args[1:]

	switch cmd {
	case "windows":
		wins := EnumAllWindows(true)
		if hasFlag(rest, "--json") {
			fmt.Println(jsonStr(wins))
		} else {
			for _, w := range wins {
				fmt.Printf("  [%8d] %-60s %s\n", w.HWND, truncate(w.Title, 60), truncate(w.Class, 30))
			}
		}

	case "screen":
		vx, vy, vw, vh, pw, ph := ScreenSize()
		fmt.Println(jsonStr(map[string]int{
			"vx": vx, "vy": vy, "width": vw, "height": vh,
			"primary_width": pw, "primary_height": ph,
		}))

	case "monitors":
		fmt.Println(jsonStr(GetMonitors()))

	case "foreground":
		hwnd := GetForeground()
		title := getWindowTitle(hwnd)
		fmt.Println(jsonStr(map[string]interface{}{"hwnd": hwnd, "title": title}))

	case "screenshot":
		path := "screenshot.png"
		var windowTitle string
		var region []int

		for i := 0; i < len(rest); i++ {
			switch rest[i] {
			case "-w", "--window":
				if i+1 < len(rest) {
					i++
					windowTitle = rest[i]
				}
			case "-r", "--region":
				if i+1 < len(rest) {
					i++
					region = parseInts(rest[i], ",")
				}
			default:
				if !strings.HasPrefix(rest[i], "-") {
					path = rest[i]
				}
			}
		}

		start := time.Now()
		if windowTitle != "" {
			hwnd, err := FindWindow(windowTitle)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Error: %s\n", err)
				os.Exit(1)
			}
			img := CaptureWindow(hwnd)
			SavePNG(img, path)
			fmt.Printf("Saved: %s (%dx%d) in %s\n", path, img.Bounds().Dx(), img.Bounds().Dy(), time.Since(start))
		} else if len(region) == 4 {
			img := CaptureRegion(region[0], region[1], region[2], region[3])
			SavePNG(img, path)
			fmt.Printf("Saved: %s (%dx%d) in %s\n", path, region[2], region[3], time.Since(start))
		} else {
			_, _, vw, vh, _, _ := ScreenSize()
			img := CaptureRegion(0, 0, vw, vh)
			SavePNG(img, path)
			fmt.Printf("Saved: %s (%dx%d) in %s\n", path, vw, vh, time.Since(start))
		}

	case "focus":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl focus <title>")
			os.Exit(1)
		}
		title := strings.Join(rest, " ")
		hwnd, err := FindWindow(title)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		FocusWindow(hwnd)
		fmt.Printf("Focused: %s (hwnd %d)\n", getWindowTitle(hwnd), hwnd)

	case "move":
		if len(rest) < 3 {
			fmt.Fprintln(os.Stderr, "Usage: winctl move <title> <x> <y> [w h]")
			os.Exit(1)
		}
		title := rest[0]
		x, _ := strconv.Atoi(rest[1])
		y, _ := strconv.Atoi(rest[2])
		hwnd, err := FindWindow(title)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		if len(rest) >= 5 {
			w, _ := strconv.Atoi(rest[3])
			h, _ := strconv.Atoi(rest[4])
			MoveAndResize(hwnd, x, y, w, h)
		} else {
			rect := GetWindowRectInfo(hwnd)
			MoveAndResize(hwnd, x, y, int(rect.Right-rect.Left), int(rect.Bottom-rect.Top))
		}
		fmt.Println("Moved")

	case "maximize":
		title := strings.Join(rest, " ")
		hwnd, err := FindWindow(title)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		MaximizeWindow(hwnd)
		fmt.Println("Maximized")

	case "minimize":
		title := strings.Join(rest, " ")
		hwnd, err := FindWindow(title)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		MinimizeWindow(hwnd)
		fmt.Println("Minimized")

	case "close":
		title := strings.Join(rest, " ")
		hwnd, err := FindWindow(title)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		CloseWindow(hwnd)
		fmt.Println("Closed")

	case "click":
		if len(rest) < 2 {
			fmt.Fprintln(os.Stderr, "Usage: winctl click <x> <y>")
			os.Exit(1)
		}
		x, _ := strconv.Atoi(rest[0])
		y, _ := strconv.Atoi(rest[1])
		ClickScreen(x, y)
		fmt.Printf("Clicked: %d, %d\n", x, y)

	case "nav":
		// Navigate foreground Chrome to a URL via Alt+D, type, Enter
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl nav <url>")
			os.Exit(1)
		}
		url := rest[0]
		Hotkey(0x12, 0x44) // Alt+D
		time.Sleep(300 * time.Millisecond)
		Hotkey(0x11, 0x41) // Ctrl+A
		time.Sleep(100 * time.Millisecond)
		TypeText(url)
		time.Sleep(200 * time.Millisecond)
		PressKey(0x0D) // Enter
		fmt.Printf("Navigated: %s\n", url)

	case "install-ext":
		// Install extension on a Chrome profile
		if len(rest) < 2 {
			fmt.Fprintln(os.Stderr, "Usage: winctl install-ext <profile-dir> <extension-path>")
			os.Exit(1)
		}
		profileDir := rest[0]
		extPath := rest[1]
		installExtension(profileDir, extPath)

	case "sleep":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl sleep <ms>")
			os.Exit(1)
		}
		ms, _ := strconv.Atoi(rest[0])
		time.Sleep(time.Duration(ms) * time.Millisecond)

	case "type":
		text := strings.Join(rest, " ")
		TypeText(text)
		fmt.Printf("Typed: %s\n", text)

	case "key":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl key <name>")
			os.Exit(1)
		}
		vk := vkFromName(rest[0])
		PressKey(vk)
		fmt.Printf("Pressed: %s\n", rest[0])

	case "hotkey":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl hotkey ctrl+c")
			os.Exit(1)
		}
		keys := strings.Split(rest[0], "+")
		var vks []uint16
		for _, k := range keys {
			vks = append(vks, vkFromName(k))
		}
		Hotkey(vks...)
		fmt.Printf("Hotkey: %s\n", rest[0])

	case "clip":
		if len(rest) > 0 && rest[0] == "set" {
			text := strings.Join(rest[1:], " ")
			ClipSet(text)
			fmt.Println("Clipboard set")
		} else {
			fmt.Println(ClipGet())
		}

	case "children":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl children <hwnd>")
			os.Exit(1)
		}
		hwndVal, _ := strconv.ParseUint(rest[0], 10, 64)
		kids := EnumChildWindowsOf(uintptr(hwndVal))
		for _, k := range kids {
			fmt.Printf("  [%8d] %-30s %s\n", k.HWND, truncate(k.Class, 30), truncate(k.Title, 50))
		}

	// --- Vision commands ---

	case "scan":
		var hwndVal uintptr
		depth := 3
		for i := 0; i < len(rest); i++ {
			if rest[i] == "--depth" && i+1 < len(rest) {
				depth, _ = strconv.Atoi(rest[i+1])
				i++
			} else if v, err := strconv.ParseUint(rest[i], 10, 64); err == nil {
				hwndVal = uintptr(v)
			}
		}
		elements, err := FullScan(hwndVal, depth)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Warning: %s\n", err)
		}
		fmt.Println(jsonStr(elements))

	case "find":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl find <name> [hwnd]")
			os.Exit(1)
		}
		name := rest[0]
		var hwndVal uintptr
		if len(rest) > 1 {
			v, _ := strconv.ParseUint(rest[1], 10, 64)
			hwndVal = uintptr(v)
		}
		elements, err := FindUIA(name, hwndVal)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		fmt.Printf("Found %d elements:\n", len(elements))
		for _, el := range elements {
			pos := ""
			if el.X != 0 || el.Y != 0 {
				pos = fmt.Sprintf(" @%d,%d %dx%d", el.X, el.Y, el.W, el.H)
			}
			pats := ""
			if len(el.Patterns) > 0 {
				pats = " [" + strings.Join(el.Patterns, ",") + "]"
			}
			fmt.Printf("  [%s] \"%s\"%s%s\n", el.Type, truncate(el.Name, 60), pos, pats)
		}

	case "invoke":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl invoke <name> [hwnd]")
			os.Exit(1)
		}
		name := rest[0]
		var hwndVal uintptr
		if len(rest) > 1 {
			v, _ := strconv.ParseUint(rest[1], 10, 64)
			hwndVal = uintptr(v)
		}
		result, err := InvokeUIA(name, hwndVal)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		fmt.Println(result)

	case "at":
		if len(rest) < 2 {
			fmt.Fprintln(os.Stderr, "Usage: winctl at <x> <y>")
			os.Exit(1)
		}
		x, _ := strconv.Atoi(rest[0])
		y, _ := strconv.Atoi(rest[1])
		el, err := UIAElementAt(x, y)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		fmt.Println(jsonStr(el))

	case "tree":
		var hwndVal uintptr
		depth := 4
		for i := 0; i < len(rest); i++ {
			if rest[i] == "--depth" && i+1 < len(rest) {
				depth, _ = strconv.Atoi(rest[i+1])
				i++
			} else if v, err := strconv.ParseUint(rest[i], 10, 64); err == nil {
				hwndVal = uintptr(v)
			}
		}
		if err := UIATree(hwndVal, depth); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}

	case "uia":
		// Pass-through to uia.exe
		cmd := exec.Command(uiaExePath(), rest...)
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			os.Exit(1)
		}

	case "cdp":
		handleCDP(rest)

	case "serve":
		port := 8421
		if len(rest) > 0 {
			port, _ = strconv.Atoi(rest[0])
		}
		srv := NewAPIServer(port)
		if err := srv.Start(); err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}

	case "--help", "-h", "help":
		usage()

	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n", cmd)
		usage()
		os.Exit(1)
	}
}

func handleCDP(args []string) {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "Usage: winctl cdp <tabs|eval|nav|shot|click|type|version>")
		os.Exit(1)
	}

	port := FindChromeDebugPort()
	if port == 0 {
		fmt.Fprintln(os.Stderr, "Error: Chrome debug port not found. Launch Chrome with --remote-debugging-port=9222")
		os.Exit(1)
	}

	cdp := NewCDP("127.0.0.1", port)
	cmd := args[0]
	rest := args[1:]

	switch cmd {
	case "tabs":
		tabs, err := cdp.Tabs()
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		for i, t := range tabs {
			fmt.Printf("  [%d] %s\n      %s\n", i, t.Title, t.URL)
		}

	case "eval":
		tabs, _ := cdp.Tabs()
		if len(tabs) == 0 {
			fmt.Fprintln(os.Stderr, "No tabs")
			os.Exit(1)
		}
		tabs[0].Connect()
		defer tabs[0].Close()
		expr := strings.Join(rest, " ")
		result, err := tabs[0].Eval(expr)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		fmt.Println(string(result))

	case "nav", "navigate":
		if len(rest) == 0 {
			fmt.Fprintln(os.Stderr, "Usage: winctl cdp nav <url>")
			os.Exit(1)
		}
		tabs, _ := cdp.Tabs()
		tabs[0].Connect()
		defer tabs[0].Close()
		tabs[0].Navigate(rest[0])
		fmt.Println("Navigated:", rest[0])

	case "shot", "screenshot":
		tabs, _ := cdp.Tabs()
		tabs[0].Connect()
		defer tabs[0].Close()
		b64, _ := tabs[0].ScreenshotB64()
		path := "cdp-screenshot.png"
		if len(rest) > 0 {
			path = rest[0]
		}
		data, _ := decodeB64(b64)
		os.WriteFile(path, data, 0644)
		fmt.Printf("Saved: %s (%d bytes)\n", path, len(data))

	case "click":
		if len(rest) < 2 {
			fmt.Fprintln(os.Stderr, "Usage: winctl cdp click <x> <y>")
			os.Exit(1)
		}
		tabs, _ := cdp.Tabs()
		tabs[0].Connect()
		defer tabs[0].Close()
		x, _ := strconv.ParseFloat(rest[0], 64)
		y, _ := strconv.ParseFloat(rest[1], 64)
		tabs[0].Click(x, y)
		fmt.Printf("Clicked: %.0f, %.0f\n", x, y)

	case "type":
		tabs, _ := cdp.Tabs()
		tabs[0].Connect()
		defer tabs[0].Close()
		text := strings.Join(rest, " ")
		tabs[0].TypeCDP(text)
		fmt.Printf("Typed: %s\n", text)

	case "version":
		v, err := cdp.Version()
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %s\n", err)
			os.Exit(1)
		}
		fmt.Println(jsonStr(v))

	default:
		fmt.Fprintf(os.Stderr, "Unknown CDP command: %s\n", cmd)
		os.Exit(1)
	}
}

// Helpers

func hasFlag(args []string, flag string) bool {
	for _, a := range args {
		if a == flag {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

func parseInts(s string, sep string) []int {
	parts := strings.Split(s, sep)
	var result []int
	for _, p := range parts {
		v, _ := strconv.Atoi(strings.TrimSpace(p))
		result = append(result, v)
	}
	return result
}

func decodeB64(s string) ([]byte, error) {
	return base64.StdEncoding.DecodeString(s)
}

func jsonStr2(v interface{}) string {
	b, _ := json.MarshalIndent(v, "", "  ")
	return string(b)
}

// --- Extension Installer ---

func installExtension(profileDir, extPath string) {
	chrome := `C:\Program Files\Google\Chrome\Application\chrome.exe`

	// Step 1: Find or launch Chrome with the profile
	fmt.Printf("[1/6] Looking for Chrome profile '%s'...\n", profileDir)

	var hwnd uintptr
	windows := EnumAllWindows(true)
	for _, w := range windows {
		if strings.Contains(w.Title, "Google Chrome") && w.Class == "Chrome_WidgetWin_1" {
			hwnd = w.HWND
			break
		}
	}

	if hwnd == 0 {
		fmt.Println("  Launching Chrome...")
		proc := exec.Command(chrome,
			fmt.Sprintf("--profile-directory=%s", profileDir),
			"chrome://extensions")
		proc.Start()
		time.Sleep(4 * time.Second)

		// Find the window
		for i := 0; i < 20; i++ {
			windows = EnumAllWindows(true)
			for _, w := range windows {
				if strings.Contains(w.Title, "Google Chrome") {
					hwnd = w.HWND
					break
				}
			}
			if hwnd != 0 {
				break
			}
			time.Sleep(500 * time.Millisecond)
		}
	}

	if hwnd == 0 {
		fmt.Fprintln(os.Stderr, "[FAIL] Could not find Chrome window")
		return
	}

	title := getWindowTitle(hwnd)
	fmt.Printf("[2/6] Using: %s (hwnd %d)\n", truncate(title, 60), hwnd)

	// Step 2: Focus and maximize on right monitor
	FocusWindow(hwnd)
	time.Sleep(300 * time.Millisecond)
	MoveAndResize(hwnd, 1920, 0, 1920, 1040)
	MaximizeWindow(hwnd)
	time.Sleep(500 * time.Millisecond)

	// Step 3: Navigate to chrome://extensions
	fmt.Println("[3/6] Navigating to chrome://extensions...")
	FocusWindow(hwnd)
	time.Sleep(200 * time.Millisecond)
	Hotkey(0x11, 0x4C) // Ctrl+L focuses address bar
	time.Sleep(500 * time.Millisecond)
	TypeText("chrome://extensions")
	time.Sleep(300 * time.Millisecond)
	PressKey(0x0D) // Enter
	time.Sleep(3 * time.Second)

	// Step 4: Open DevTools Console and execute JS to click "Load unpacked"
	fmt.Println("[4/6] Opening Console and clicking Load unpacked...")

	// Close DevTools if already open
	FocusWindow(hwnd)
	time.Sleep(200 * time.Millisecond)
	PressKey(0x7B) // F12 toggles DevTools
	time.Sleep(1 * time.Second)

	// Re-focus and open Console specifically (Ctrl+Shift+J)
	FocusWindow(hwnd)
	time.Sleep(300 * time.Millisecond)
	Hotkey(0x11, 0x10, 0x4A) // Ctrl+Shift+J
	time.Sleep(2 * time.Second)

	// Re-focus Chrome (Console opening may shift focus)
	FocusWindow(hwnd)
	time.Sleep(300 * time.Millisecond)

	// Set clipboard to the shadow DOM JS
	js := `document.querySelector('extensions-manager').shadowRoot.querySelector('extensions-toolbar').shadowRoot.querySelector('#loadUnpacked').click()`
	ClipSet(js)
	time.Sleep(200 * time.Millisecond)

	// Paste into Console
	FocusWindow(hwnd)
	time.Sleep(200 * time.Millisecond)
	Hotkey(0x11, 0x56) // Ctrl+V
	time.Sleep(500 * time.Millisecond)

	// Dismiss autocomplete
	PressKey(0x1B) // Escape
	time.Sleep(200 * time.Millisecond)

	// Execute
	PressKey(0x0D) // Enter
	fmt.Println("[5/6] JS executed, waiting for folder dialog...")
	time.Sleep(3 * time.Second)

	// Step 5: Handle folder picker dialog
	fmt.Println("[6/6] Handling folder picker...")
	for i := 0; i < 15; i++ {
		dlg := FindWindowByClass("#32770")
		if dlg != 0 {
			dlgTitle := getWindowTitle(dlg)
			fmt.Printf("  Dialog found: %s\n", dlgTitle)
			FocusWindow(dlg)
			time.Sleep(500 * time.Millisecond)

			// Alt+D to focus address bar in folder dialog
			Hotkey(0x12, 0x44) // Alt+D
			time.Sleep(400 * time.Millisecond)
			// Select all, type path
			Hotkey(0x11, 0x41) // Ctrl+A
			time.Sleep(100 * time.Millisecond)
			TypeText(extPath)
			time.Sleep(400 * time.Millisecond)
			PressKey(0x0D) // Enter to navigate to folder
			time.Sleep(1500 * time.Millisecond)
			PressKey(0x0D) // Enter again to click "Select Folder"
			time.Sleep(2 * time.Second)

			// Close DevTools
			FocusWindow(hwnd)
			time.Sleep(300 * time.Millisecond)
			PressKey(0x7B) // F12 toggles DevTools off
			time.Sleep(500 * time.Millisecond)

			fmt.Println("[OK] Extension loaded!")
			return
		}
		time.Sleep(500 * time.Millisecond)
	}

	// Fallback: close DevTools anyway
	FocusWindow(hwnd)
	time.Sleep(200 * time.Millisecond)
	PressKey(0x7B) // F12
	time.Sleep(500 * time.Millisecond)
	fmt.Println("[?] No folder dialog appeared - check if extension is already installed")
}
