package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image/png"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

// HTTP API server — lets Copilot CLI call native winctl over HTTP with near-zero latency

type APIServer struct {
	port int
}

func NewAPIServer(port int) *APIServer {
	return &APIServer{port: port}
}

func (s *APIServer) Start() error {
	mux := http.NewServeMux()

	// Desktop
	mux.HandleFunc("/screen", s.handleScreen)
	mux.HandleFunc("/monitors", s.handleMonitors)
	mux.HandleFunc("/windows", s.handleWindows)
	mux.HandleFunc("/foreground", s.handleForeground)
	mux.HandleFunc("/screenshot", s.handleScreenshot)
	mux.HandleFunc("/screenshot/window", s.handleScreenshotWindow)
	mux.HandleFunc("/screenshot/region", s.handleScreenshotRegion)
	mux.HandleFunc("/focus", s.handleFocus)
	mux.HandleFunc("/move", s.handleMove)
	mux.HandleFunc("/maximize", s.handleMaximize)
	mux.HandleFunc("/minimize", s.handleMinimize)
	mux.HandleFunc("/close", s.handleClose)
	mux.HandleFunc("/type", s.handleType)
	mux.HandleFunc("/key", s.handleKey)
	mux.HandleFunc("/hotkey", s.handleHotkey)
	mux.HandleFunc("/clip", s.handleClip)
	mux.HandleFunc("/clip/set", s.handleClipSet)

	// CDP
	mux.HandleFunc("/cdp/tabs", s.handleCDPTabs)
	mux.HandleFunc("/cdp/eval", s.handleCDPEval)
	mux.HandleFunc("/cdp/navigate", s.handleCDPNavigate)
	mux.HandleFunc("/cdp/screenshot", s.handleCDPScreenshot)
	mux.HandleFunc("/cdp/click", s.handleCDPClick)
	mux.HandleFunc("/cdp/type", s.handleCDPType)
	mux.HandleFunc("/cdp/version", s.handleCDPVersion)

	// Health
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]interface{}{"status": "ok", "uptime": time.Since(startTime).Seconds()})
	})

	addr := fmt.Sprintf("127.0.0.1:%d", s.port)
	fmt.Printf("winctl native API on http://%s\n", addr)
	return http.ListenAndServe(addr, mux)
}

var startTime = time.Now()

func respondJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func respondError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

// --- Desktop handlers ---

func (s *APIServer) handleScreen(w http.ResponseWriter, r *http.Request) {
	vx, vy, vw, vh, pw, ph := ScreenSize()
	respondJSON(w, map[string]int{"vx": vx, "vy": vy, "width": vw, "height": vh, "primary_width": pw, "primary_height": ph})
}

func (s *APIServer) handleMonitors(w http.ResponseWriter, r *http.Request) {
	respondJSON(w, GetMonitors())
}

func (s *APIServer) handleWindows(w http.ResponseWriter, r *http.Request) {
	wins := EnumAllWindows(true)
	type WinOut struct {
		HWND  uintptr `json:"hwnd"`
		Title string  `json:"title"`
		Class string  `json:"class"`
		PID   uint32  `json:"pid"`
	}
	var out []WinOut
	for _, w := range wins {
		out = append(out, WinOut{w.HWND, w.Title, w.Class, w.PID})
	}
	respondJSON(w, out)
}

func (s *APIServer) handleForeground(w http.ResponseWriter, r *http.Request) {
	hwnd := GetForeground()
	title := getWindowTitle(hwnd)
	respondJSON(w, map[string]interface{}{"hwnd": hwnd, "title": title})
}

func (s *APIServer) handleScreenshot(w http.ResponseWriter, r *http.Request) {
	_, _, vw, vh, _, _ := ScreenSize()
	vx, _ := strconv.Atoi(r.URL.Query().Get("x"))
	vy, _ := strconv.Atoi(r.URL.Query().Get("y"))
	if r.URL.Query().Get("w") != "" {
		vw, _ = strconv.Atoi(r.URL.Query().Get("w"))
	}
	if r.URL.Query().Get("h") != "" {
		vh, _ = strconv.Atoi(r.URL.Query().Get("h"))
	}
	img := CaptureRegion(vx, vy, vw, vh)
	path := r.URL.Query().Get("path")
	if path != "" {
		SavePNG(img, path)
		respondJSON(w, map[string]interface{}{"path": path, "width": vw, "height": vh})
		return
	}
	w.Header().Set("Content-Type", "image/png")
	png.Encode(w, img)
}

func (s *APIServer) handleScreenshotWindow(w http.ResponseWriter, r *http.Request) {
	title := r.URL.Query().Get("title")
	if title == "" {
		respondError(w, 400, "title required")
		return
	}
	hwnd, err := FindWindow(title)
	if err != nil {
		respondError(w, 404, err.Error())
		return
	}
	img := CaptureWindow(hwnd)
	if img == nil {
		respondError(w, 500, "capture failed")
		return
	}
	path := r.URL.Query().Get("path")
	if path != "" {
		SavePNG(img, path)
		rect := GetWindowRectInfo(hwnd)
		respondJSON(w, map[string]interface{}{"path": path, "width": rect.Right - rect.Left, "height": rect.Bottom - rect.Top})
		return
	}
	w.Header().Set("Content-Type", "image/png")
	png.Encode(w, img)
}

func (s *APIServer) handleScreenshotRegion(w http.ResponseWriter, r *http.Request) {
	x, _ := strconv.Atoi(r.URL.Query().Get("x"))
	y, _ := strconv.Atoi(r.URL.Query().Get("y"))
	rw, _ := strconv.Atoi(r.URL.Query().Get("w"))
	rh, _ := strconv.Atoi(r.URL.Query().Get("h"))
	if rw <= 0 || rh <= 0 {
		respondError(w, 400, "w and h required")
		return
	}
	img := CaptureRegion(x, y, rw, rh)
	path := r.URL.Query().Get("path")
	if path != "" {
		SavePNG(img, path)
		respondJSON(w, map[string]interface{}{"path": path, "width": rw, "height": rh})
		return
	}
	w.Header().Set("Content-Type", "image/png")
	png.Encode(w, img)
}

func (s *APIServer) handleFocus(w http.ResponseWriter, r *http.Request) {
	title := r.URL.Query().Get("title")
	hwnd, err := FindWindow(title)
	if err != nil {
		respondError(w, 404, err.Error())
		return
	}
	FocusWindow(hwnd)
	respondJSON(w, map[string]interface{}{"hwnd": hwnd, "title": getWindowTitle(hwnd)})
}

func (s *APIServer) handleMove(w http.ResponseWriter, r *http.Request) {
	title := r.URL.Query().Get("title")
	x, _ := strconv.Atoi(r.URL.Query().Get("x"))
	y, _ := strconv.Atoi(r.URL.Query().Get("y"))
	mw, _ := strconv.Atoi(r.URL.Query().Get("w"))
	mh, _ := strconv.Atoi(r.URL.Query().Get("h"))
	hwnd, err := FindWindow(title)
	if err != nil {
		respondError(w, 404, err.Error())
		return
	}
	if mw > 0 && mh > 0 {
		MoveAndResize(hwnd, x, y, mw, mh)
	} else {
		rect := GetWindowRectInfo(hwnd)
		MoveAndResize(hwnd, x, y, int(rect.Right-rect.Left), int(rect.Bottom-rect.Top))
	}
	respondJSON(w, map[string]string{"status": "moved"})
}

func (s *APIServer) handleMaximize(w http.ResponseWriter, r *http.Request) {
	title := r.URL.Query().Get("title")
	hwnd, err := FindWindow(title)
	if err != nil {
		respondError(w, 404, err.Error())
		return
	}
	MaximizeWindow(hwnd)
	respondJSON(w, map[string]string{"status": "maximized"})
}

func (s *APIServer) handleMinimize(w http.ResponseWriter, r *http.Request) {
	title := r.URL.Query().Get("title")
	hwnd, err := FindWindow(title)
	if err != nil {
		respondError(w, 404, err.Error())
		return
	}
	MinimizeWindow(hwnd)
	respondJSON(w, map[string]string{"status": "minimized"})
}

func (s *APIServer) handleClose(w http.ResponseWriter, r *http.Request) {
	title := r.URL.Query().Get("title")
	hwnd, err := FindWindow(title)
	if err != nil {
		respondError(w, 404, err.Error())
		return
	}
	CloseWindow(hwnd)
	respondJSON(w, map[string]string{"status": "closed"})
}

func (s *APIServer) handleType(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	var req struct {
		Text string `json:"text"`
	}
	json.Unmarshal(body, &req)
	if req.Text == "" {
		req.Text = r.URL.Query().Get("text")
	}
	TypeText(req.Text)
	respondJSON(w, map[string]string{"status": "typed", "text": req.Text})
}

func (s *APIServer) handleKey(w http.ResponseWriter, r *http.Request) {
	key := r.URL.Query().Get("key")
	vk := vkFromName(key)
	PressKey(vk)
	respondJSON(w, map[string]string{"status": "pressed", "key": key})
}

func (s *APIServer) handleHotkey(w http.ResponseWriter, r *http.Request) {
	keys := strings.Split(r.URL.Query().Get("keys"), "+")
	var vks []uint16
	for _, k := range keys {
		vks = append(vks, vkFromName(strings.TrimSpace(k)))
	}
	Hotkey(vks...)
	respondJSON(w, map[string]string{"status": "pressed"})
}

func (s *APIServer) handleClip(w http.ResponseWriter, r *http.Request) {
	respondJSON(w, map[string]string{"text": ClipGet()})
}

func (s *APIServer) handleClipSet(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	var req struct {
		Text string `json:"text"`
	}
	json.Unmarshal(body, &req)
	if req.Text == "" {
		req.Text = r.URL.Query().Get("text")
	}
	ClipSet(req.Text)
	respondJSON(w, map[string]string{"status": "set"})
}

// --- CDP handlers ---

func getCDP(r *http.Request) (*CDP, error) {
	port := FindChromeDebugPort()
	if r.URL.Query().Get("port") != "" {
		port = parsePort(r.URL.Query().Get("port"))
	}
	if port == 0 {
		return nil, fmt.Errorf("Chrome debug port not found")
	}
	return NewCDP("127.0.0.1", port), nil
}

func (s *APIServer) handleCDPVersion(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	v, err := cdp.Version()
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	respondJSON(w, v)
}

func (s *APIServer) handleCDPTabs(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	tabs, err := cdp.Tabs()
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	type TabOut struct {
		ID    string `json:"id"`
		Title string `json:"title"`
		URL   string `json:"url"`
	}
	var out []TabOut
	for _, t := range tabs {
		out = append(out, TabOut{t.ID, t.Title, t.URL})
	}
	respondJSON(w, out)
}

func (s *APIServer) handleCDPEval(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	tabs, err := cdp.Tabs()
	if err != nil || len(tabs) == 0 {
		respondError(w, 500, "no tabs")
		return
	}
	tab := tabs[0]
	tabIdx := r.URL.Query().Get("tab")
	if tabIdx != "" {
		i, _ := strconv.Atoi(tabIdx)
		if i < len(tabs) {
			tab = tabs[i]
		}
	}
	tab.Connect()
	defer tab.Close()

	body, _ := io.ReadAll(r.Body)
	var req struct {
		Expr string `json:"expr"`
	}
	json.Unmarshal(body, &req)
	if req.Expr == "" {
		req.Expr = r.URL.Query().Get("expr")
	}

	result, err := tab.Eval(req.Expr)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Write(result)
}

func (s *APIServer) handleCDPNavigate(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	tabs, err := cdp.Tabs()
	if err != nil || len(tabs) == 0 {
		respondError(w, 500, "no tabs")
		return
	}
	tab := tabs[0]
	tab.Connect()
	defer tab.Close()

	url := r.URL.Query().Get("url")
	tab.Navigate(url)
	time.Sleep(100 * time.Millisecond)
	respondJSON(w, map[string]string{"status": "navigated", "url": url})
}

func (s *APIServer) handleCDPScreenshot(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	tabs, err := cdp.Tabs()
	if err != nil || len(tabs) == 0 {
		respondError(w, 500, "no tabs")
		return
	}
	tab := tabs[0]
	tab.Connect()
	defer tab.Close()

	b64, err := tab.ScreenshotB64()
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}

	path := r.URL.Query().Get("path")
	if path != "" {
		data, _ := base64.StdEncoding.DecodeString(b64)
		os.WriteFile(path, data, 0644)
		respondJSON(w, map[string]interface{}{"path": path, "size": len(data)})
		return
	}

	data, _ := base64.StdEncoding.DecodeString(b64)
	w.Header().Set("Content-Type", "image/png")
	w.Write(data)
}

func (s *APIServer) handleCDPClick(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	tabs, err := cdp.Tabs()
	if err != nil || len(tabs) == 0 {
		respondError(w, 500, "no tabs")
		return
	}
	tab := tabs[0]
	tab.Connect()
	defer tab.Close()

	x, _ := strconv.ParseFloat(r.URL.Query().Get("x"), 64)
	y, _ := strconv.ParseFloat(r.URL.Query().Get("y"), 64)
	tab.Click(x, y)
	respondJSON(w, map[string]string{"status": "clicked"})
}

func (s *APIServer) handleCDPType(w http.ResponseWriter, r *http.Request) {
	cdp, err := getCDP(r)
	if err != nil {
		respondError(w, 500, err.Error())
		return
	}
	tabs, err := cdp.Tabs()
	if err != nil || len(tabs) == 0 {
		respondError(w, 500, "no tabs")
		return
	}
	tab := tabs[0]
	tab.Connect()
	defer tab.Close()

	body, _ := io.ReadAll(r.Body)
	var req struct {
		Text string `json:"text"`
	}
	json.Unmarshal(body, &req)
	if req.Text == "" {
		req.Text = r.URL.Query().Get("text")
	}
	tab.TypeCDP(req.Text)
	respondJSON(w, map[string]string{"status": "typed"})
}

// VK name map
func vkFromName(name string) uint16 {
	name = strings.ToLower(strings.TrimSpace(name))
	vkMap := map[string]uint16{
		"enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
		"backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
		"pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
		"space": 0x20, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
		"win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
		"f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
		"f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
		"a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46,
		"g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C,
		"m": 0x4D, "n": 0x4E, "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52,
		"s": 0x53, "t": 0x54, "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58,
		"y": 0x59, "z": 0x5A,
		"0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
		"5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
		"printscreen": 0x2C, "scrolllock": 0x91, "pause": 0x13, "capslock": 0x14, "numlock": 0x90,
	}
	if vk, ok := vkMap[name]; ok {
		return vk
	}
	if len(name) == 1 {
		return uint16(name[0])
	}
	return 0
}
