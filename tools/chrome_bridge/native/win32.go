package main

import (
	"fmt"
	"image"
	"image/png"
	"os"
	"strings"
	"syscall"
	"unsafe"
)

var (
	user32   = syscall.NewLazyDLL("user32.dll")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	gdi32    = syscall.NewLazyDLL("gdi32.dll")

	// user32
	enumWindows           = user32.NewProc("EnumWindows")
	getWindowTextW        = user32.NewProc("GetWindowTextW")
	getWindowTextLengthW  = user32.NewProc("GetWindowTextLengthW")
	getClassNameW         = user32.NewProc("GetClassNameW")
	isWindowVisible       = user32.NewProc("IsWindowVisible")
	getForegroundWindow   = user32.NewProc("GetForegroundWindow")
	setForegroundWindow   = user32.NewProc("SetForegroundWindow")
	showWindow            = user32.NewProc("ShowWindow")
	moveWindow            = user32.NewProc("MoveWindow")
	getWindowRect         = user32.NewProc("GetWindowRect")
	getClientRect         = user32.NewProc("GetClientRect")
	getDesktopWindow      = user32.NewProc("GetDesktopWindow")
	getDC                 = user32.NewProc("GetDC")
	releaseDC             = user32.NewProc("ReleaseDC")
	getSystemMetrics      = user32.NewProc("GetSystemMetrics")
	sendInput             = user32.NewProc("SendInput")
	postMessageW          = user32.NewProc("PostMessageW")
	sendMessageW          = user32.NewProc("SendMessageW")
	openClipboard         = user32.NewProc("OpenClipboard")
	closeClipboard        = user32.NewProc("CloseClipboard")
	emptyClipboard        = user32.NewProc("EmptyClipboard")
	setClipboardData      = user32.NewProc("SetClipboardData")
	getClipboardData      = user32.NewProc("GetClipboardData")
	printWindow           = user32.NewProc("PrintWindow")
	enumDisplayMonitors   = user32.NewProc("EnumDisplayMonitors")
	getMonitorInfoW       = user32.NewProc("GetMonitorInfoW")
	setWindowPos          = user32.NewProc("SetWindowPos")
	getWindowThreadProcessId = user32.NewProc("GetWindowThreadProcessId")

	// gdi32
	createCompatibleDC     = gdi32.NewProc("CreateCompatibleDC")
	createCompatibleBitmap = gdi32.NewProc("CreateCompatibleBitmap")
	selectObject           = gdi32.NewProc("SelectObject")
	bitBlt                 = gdi32.NewProc("BitBlt")
	deleteDC               = gdi32.NewProc("DeleteDC")
	deleteObject           = gdi32.NewProc("DeleteObject")
	getDIBits              = gdi32.NewProc("GetDIBits")

	// kernel32
	globalAlloc   = kernel32.NewProc("GlobalAlloc")
	globalLock    = kernel32.NewProc("GlobalLock")
	globalUnlock  = kernel32.NewProc("GlobalUnlock")
	globalSize    = kernel32.NewProc("GlobalSize")
)

const (
	SW_RESTORE       = 9
	SW_MAXIMIZE      = 3
	SW_MINIMIZE      = 6
	SW_SHOW          = 5
	SM_CXSCREEN      = 0
	SM_CYSCREEN      = 1
	SM_XVIRTUALSCREEN = 76
	SM_YVIRTUALSCREEN = 77
	SM_CXVIRTUALSCREEN = 78
	SM_CYVIRTUALSCREEN = 79
	SRCCOPY          = 0x00CC0020
	BI_RGB           = 0
	DIB_RGB_COLORS   = 0
	CF_UNICODETEXT   = 13
	GMEM_MOVEABLE    = 0x0002
	INPUT_KEYBOARD   = 1
	KEYEVENTF_UNICODE  = 0x0004
	KEYEVENTF_KEYUP    = 0x0002
	WM_CLOSE         = 0x0010
	SWP_NOZORDER     = 0x0004
	SWP_NOACTIVATE   = 0x0010
	HWND_TOP         = 0
	PW_RENDERFULLCONTENT = 2
)

type RECT struct {
	Left, Top, Right, Bottom int32
}

type BITMAPINFOHEADER struct {
	BiSize          uint32
	BiWidth         int32
	BiHeight        int32
	BiPlanes        uint16
	BiBitCount      uint16
	BiCompression   uint32
	BiSizeImage     uint32
	BiXPelsPerMeter int32
	BiYPelsPerMeter int32
	BiClrUsed       uint32
	BiClrImportant  uint32
}

type MONITORINFO struct {
	CbSize    uint32
	RcMonitor RECT
	RcWork    RECT
	DwFlags   uint32
}

type KEYBDINPUT struct {
	WVk         uint16
	WScan       uint16
	DwFlags     uint32
	Time        uint32
	DwExtraInfo uintptr
}

type INPUT struct {
	Type uint32
	Ki   KEYBDINPUT
	_    [8]byte // padding
}

type WindowInfo struct {
	HWND  uintptr
	Title string
	Class string
	PID   uint32
}

type MonitorInfo struct {
	Name    string
	X, Y    int
	W, H    int
	Primary bool
}

// --- Window Management ---

func EnumAllWindows(visibleOnly bool) []WindowInfo {
	var windows []WindowInfo
	cb := syscall.NewCallback(func(hwnd uintptr, _ uintptr) uintptr {
		if visibleOnly {
			vis, _, _ := isWindowVisible.Call(hwnd)
			if vis == 0 {
				return 1
			}
		}
		title := getWindowTitle(hwnd)
		if title == "" {
			return 1
		}
		cls := getWindowClass(hwnd)
		var pid uint32
		getWindowThreadProcessId.Call(hwnd, uintptr(unsafe.Pointer(&pid)))
		windows = append(windows, WindowInfo{HWND: hwnd, Title: title, Class: cls, PID: pid})
		return 1
	})
	enumWindows.Call(cb, 0)
	return windows
}

func getWindowTitle(hwnd uintptr) string {
	length, _, _ := getWindowTextLengthW.Call(hwnd)
	if length == 0 {
		return ""
	}
	buf := make([]uint16, length+1)
	getWindowTextW.Call(hwnd, uintptr(unsafe.Pointer(&buf[0])), length+1)
	return syscall.UTF16ToString(buf)
}

func getWindowClass(hwnd uintptr) string {
	buf := make([]uint16, 256)
	getClassNameW.Call(hwnd, uintptr(unsafe.Pointer(&buf[0])), 256)
	return syscall.UTF16ToString(buf)
}

func FindWindow(titleMatch string) (uintptr, error) {
	titleMatch = strings.ToLower(titleMatch)
	windows := EnumAllWindows(true)
	for _, w := range windows {
		if strings.Contains(strings.ToLower(w.Title), titleMatch) {
			return w.HWND, nil
		}
	}
	return 0, fmt.Errorf("window not found: %s", titleMatch)
}

func FocusWindow(hwnd uintptr) {
	// Alt-key trick: pressing Alt allows SetForegroundWindow to succeed
	// even when called from a background process (bypasses focus-steal prevention)
	inputs := [2]INPUT{
		{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WVk: 0x12}},                             // Alt down
		{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WVk: 0x12, DwFlags: KEYEVENTF_KEYUP}},   // Alt up
	}
	sendInput.Call(2, uintptr(unsafe.Pointer(&inputs[0])), unsafe.Sizeof(inputs[0]))
	showWindow.Call(hwnd, SW_RESTORE)
	setForegroundWindow.Call(hwnd)
}

func MoveAndResize(hwnd uintptr, x, y, w, h int) {
	moveWindow.Call(hwnd, uintptr(x), uintptr(y), uintptr(w), uintptr(h), 1)
}

func MaximizeWindow(hwnd uintptr) {
	showWindow.Call(hwnd, SW_MAXIMIZE)
}

func MinimizeWindow(hwnd uintptr) {
	showWindow.Call(hwnd, SW_MINIMIZE)
}

func CloseWindow(hwnd uintptr) {
	postMessageW.Call(hwnd, WM_CLOSE, 0, 0)
}

func GetWindowRectInfo(hwnd uintptr) RECT {
	var r RECT
	getWindowRect.Call(hwnd, uintptr(unsafe.Pointer(&r)))
	return r
}

func GetForeground() uintptr {
	hwnd, _, _ := getForegroundWindow.Call()
	return hwnd
}

// --- Screen ---

func ScreenSize() (int, int, int, int, int, int) {
	vx, _, _ := getSystemMetrics.Call(SM_XVIRTUALSCREEN)
	vy, _, _ := getSystemMetrics.Call(SM_YVIRTUALSCREEN)
	vw, _, _ := getSystemMetrics.Call(SM_CXVIRTUALSCREEN)
	vh, _, _ := getSystemMetrics.Call(SM_CYVIRTUALSCREEN)
	pw, _, _ := getSystemMetrics.Call(SM_CXSCREEN)
	ph, _, _ := getSystemMetrics.Call(SM_CYSCREEN)
	return int(vx), int(vy), int(vw), int(vh), int(pw), int(ph)
}

func GetMonitors() []MonitorInfo {
	var monitors []MonitorInfo
	idx := 0
	cb := syscall.NewCallback(func(hMonitor uintptr, hdc uintptr, lpRect uintptr, _ uintptr) uintptr {
		var mi MONITORINFO
		mi.CbSize = uint32(unsafe.Sizeof(mi))
		getMonitorInfoW.Call(hMonitor, uintptr(unsafe.Pointer(&mi)))
		primary := mi.DwFlags&1 != 0
		idx++
		monitors = append(monitors, MonitorInfo{
			Name:    fmt.Sprintf("\\\\.\\DISPLAY%d", idx),
			X:       int(mi.RcMonitor.Left),
			Y:       int(mi.RcMonitor.Top),
			W:       int(mi.RcMonitor.Right - mi.RcMonitor.Left),
			H:       int(mi.RcMonitor.Bottom - mi.RcMonitor.Top),
			Primary: primary,
		})
		return 1
	})
	enumDisplayMonitors.Call(0, 0, cb, 0)
	return monitors
}

// --- Screenshot ---

func CaptureRegion(x, y, w, h int) *image.RGBA {
	desktop, _, _ := getDesktopWindow.Call()
	hdc, _, _ := getDC.Call(desktop)
	defer releaseDC.Call(desktop, hdc)

	memDC, _, _ := createCompatibleDC.Call(hdc)
	defer deleteDC.Call(memDC)

	bmp, _, _ := createCompatibleBitmap.Call(hdc, uintptr(w), uintptr(h))
	defer deleteObject.Call(bmp)

	selectObject.Call(memDC, bmp)
	bitBlt.Call(memDC, 0, 0, uintptr(w), uintptr(h), hdc, uintptr(x), uintptr(y), SRCCOPY)

	// Extract pixels
	bmi := BITMAPINFOHEADER{
		BiSize:        uint32(unsafe.Sizeof(BITMAPINFOHEADER{})),
		BiWidth:       int32(w),
		BiHeight:      -int32(h), // top-down
		BiPlanes:      1,
		BiBitCount:    32,
		BiCompression: BI_RGB,
	}
	pixels := make([]byte, w*h*4)
	getDIBits.Call(memDC, bmp, 0, uintptr(h), uintptr(unsafe.Pointer(&pixels[0])), uintptr(unsafe.Pointer(&bmi)), DIB_RGB_COLORS)

	img := image.NewRGBA(image.Rect(0, 0, w, h))
	// BGRA -> RGBA
	for i := 0; i < len(pixels); i += 4 {
		img.Pix[i+0] = pixels[i+2] // R
		img.Pix[i+1] = pixels[i+1] // G
		img.Pix[i+2] = pixels[i+0] // B
		img.Pix[i+3] = 255         // A
	}
	return img
}

func CaptureWindow(hwnd uintptr) *image.RGBA {
	r := GetWindowRectInfo(hwnd)
	w := int(r.Right - r.Left)
	h := int(r.Bottom - r.Top)
	if w <= 0 || h <= 0 {
		return nil
	}

	hdc, _, _ := getDC.Call(hwnd)
	defer releaseDC.Call(hwnd, hdc)

	memDC, _, _ := createCompatibleDC.Call(hdc)
	defer deleteDC.Call(memDC)

	bmp, _, _ := createCompatibleBitmap.Call(hdc, uintptr(w), uintptr(h))
	defer deleteObject.Call(bmp)

	selectObject.Call(memDC, bmp)
	printWindow.Call(hwnd, memDC, PW_RENDERFULLCONTENT)

	bmi := BITMAPINFOHEADER{
		BiSize:        uint32(unsafe.Sizeof(BITMAPINFOHEADER{})),
		BiWidth:       int32(w),
		BiHeight:      -int32(h),
		BiPlanes:      1,
		BiBitCount:    32,
		BiCompression: BI_RGB,
	}
	pixels := make([]byte, w*h*4)
	getDIBits.Call(memDC, bmp, 0, uintptr(h), uintptr(unsafe.Pointer(&pixels[0])), uintptr(unsafe.Pointer(&bmi)), DIB_RGB_COLORS)

	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for i := 0; i < len(pixels); i += 4 {
		img.Pix[i+0] = pixels[i+2]
		img.Pix[i+1] = pixels[i+1]
		img.Pix[i+2] = pixels[i+0]
		img.Pix[i+3] = 255
	}
	return img
}

func SavePNG(img *image.RGBA, path string) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return png.Encode(f, img)
}

// --- Keyboard (virtual, no physical interference) ---

func TypeText(text string) {
	runes := []rune(text)
	inputs := make([]INPUT, len(runes)*2)
	for i, r := range runes {
		inputs[i*2] = INPUT{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WScan: uint16(r), DwFlags: KEYEVENTF_UNICODE}}
		inputs[i*2+1] = INPUT{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WScan: uint16(r), DwFlags: KEYEVENTF_UNICODE | KEYEVENTF_KEYUP}}
	}
	sendInput.Call(uintptr(len(inputs)), uintptr(unsafe.Pointer(&inputs[0])), unsafe.Sizeof(inputs[0]))
}

func PressKey(vk uint16) {
	inputs := [2]INPUT{
		{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WVk: vk}},
		{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WVk: vk, DwFlags: KEYEVENTF_KEYUP}},
	}
	sendInput.Call(2, uintptr(unsafe.Pointer(&inputs[0])), unsafe.Sizeof(inputs[0]))
}

func Hotkey(vks ...uint16) {
	inputs := make([]INPUT, len(vks)*2)
	for i, vk := range vks {
		inputs[i] = INPUT{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WVk: vk}}
	}
	for i, vk := range vks {
		inputs[len(vks)+i] = INPUT{Type: INPUT_KEYBOARD, Ki: KEYBDINPUT{WVk: vk, DwFlags: KEYEVENTF_KEYUP}}
	}
	sendInput.Call(uintptr(len(inputs)), uintptr(unsafe.Pointer(&inputs[0])), unsafe.Sizeof(inputs[0]))
}

// --- Mouse (virtual, no physical interference) ---

type MOUSEINPUT struct {
	Dx          int32
	Dy          int32
	MouseData   uint32
	DwFlags     uint32
	Time        uint32
	DwExtraInfo uintptr
}

type MOUSEINPUT_INPUT struct {
	Type uint32
	Mi   MOUSEINPUT
	_    [8]byte
}

const (
	INPUT_MOUSE             = 0
	MOUSEEVENTF_MOVE        = 0x0001
	MOUSEEVENTF_LEFTDOWN    = 0x0002
	MOUSEEVENTF_LEFTUP      = 0x0004
	MOUSEEVENTF_ABSOLUTE    = 0x8000
	MOUSEEVENTF_VIRTUALDESK = 0x4000
)

func ClickScreen(x, y int) {
	// Normalize to 0-65535
	vw, _, _ := getSystemMetrics.Call(SM_CXVIRTUALSCREEN)
	vh, _, _ := getSystemMetrics.Call(SM_CYVIRTUALSCREEN)
	vx, _, _ := getSystemMetrics.Call(SM_XVIRTUALSCREEN)
	vy, _, _ := getSystemMetrics.Call(SM_YVIRTUALSCREEN)

	nx := int32((x - int(vx)) * 65535 / int(vw))
	ny := int32((y - int(vy)) * 65535 / int(vh))

	flags := uint32(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)

	inputs := [3]MOUSEINPUT_INPUT{
		{Type: INPUT_MOUSE, Mi: MOUSEINPUT{Dx: nx, Dy: ny, DwFlags: flags}},
		{Type: INPUT_MOUSE, Mi: MOUSEINPUT{Dx: nx, Dy: ny, DwFlags: flags | MOUSEEVENTF_LEFTDOWN}},
		{Type: INPUT_MOUSE, Mi: MOUSEINPUT{Dx: nx, Dy: ny, DwFlags: flags | MOUSEEVENTF_LEFTUP}},
	}
	sendInput.Call(3, uintptr(unsafe.Pointer(&inputs[0])), unsafe.Sizeof(inputs[0]))
}

func MouseMove(x, y int) {
	vw, _, _ := getSystemMetrics.Call(SM_CXVIRTUALSCREEN)
	vh, _, _ := getSystemMetrics.Call(SM_CYVIRTUALSCREEN)
	vx, _, _ := getSystemMetrics.Call(SM_XVIRTUALSCREEN)
	vy, _, _ := getSystemMetrics.Call(SM_YVIRTUALSCREEN)
	nx := int32((x - int(vx)) * 65535 / int(vw))
	ny := int32((y - int(vy)) * 65535 / int(vh))
	flags := uint32(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)
	inputs := [1]MOUSEINPUT_INPUT{
		{Type: INPUT_MOUSE, Mi: MOUSEINPUT{Dx: nx, Dy: ny, DwFlags: flags}},
	}
	sendInput.Call(1, uintptr(unsafe.Pointer(&inputs[0])), unsafe.Sizeof(inputs[0]))
}

// FindWindowByClass finds a window by class name (e.g. "#32770" for dialogs)
func FindWindowByClass(class string) uintptr {
	findWindowW := user32.NewProc("FindWindowW")
	classPtr, _ := syscall.UTF16PtrFromString(class)
	hwnd, _, _ := findWindowW.Call(uintptr(unsafe.Pointer(classPtr)), 0)
	return hwnd
}

// --- Clipboard ---

func ClipSet(text string) error {
	utf16, err := syscall.UTF16FromString(text)
	if err != nil {
		return err
	}
	openClipboard.Call(0)
	emptyClipboard.Call()

	size := len(utf16) * 2
	hMem, _, _ := globalAlloc.Call(GMEM_MOVEABLE, uintptr(size))
	ptr, _, _ := globalLock.Call(hMem)
	copy((*[1 << 28]uint16)(unsafe.Pointer(ptr))[:len(utf16)], utf16)
	globalUnlock.Call(hMem)
	setClipboardData.Call(CF_UNICODETEXT, hMem)
	closeClipboard.Call()
	return nil
}

func ClipGet() string {
	openClipboard.Call(0)
	defer closeClipboard.Call()

	h, _, _ := getClipboardData.Call(CF_UNICODETEXT)
	if h == 0 {
		return ""
	}
	ptr, _, _ := globalLock.Call(h)
	if ptr == 0 {
		return ""
	}
	defer globalUnlock.Call(h)

	sz, _, _ := globalSize.Call(h)
	if sz == 0 {
		return ""
	}
	n := int(sz) / 2
	buf := make([]uint16, n)
	copy(buf, (*[1 << 28]uint16)(unsafe.Pointer(ptr))[:n])
	return syscall.UTF16ToString(buf)
}
