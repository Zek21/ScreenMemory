package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

// CDP — Chrome DevTools Protocol client, pure Go, zero overhead

type CDPTab struct {
	ID        string
	Title     string
	URL       string
	Type      string
	WsURL     string
	conn      *websocket.Conn
	mu        sync.Mutex
	msgID     atomic.Int64
	pending   map[int64]chan json.RawMessage
	pendingMu sync.Mutex
	events    map[string][]func(json.RawMessage)
	eventsMu  sync.RWMutex
}

type CDP struct {
	Host string
	Port int
	tabs []*CDPTab
}

func NewCDP(host string, port int) *CDP {
	return &CDP{Host: host, Port: port}
}

func (c *CDP) baseURL() string {
	return fmt.Sprintf("http://%s:%d", c.Host, c.Port)
}

func (c *CDP) Tabs() ([]*CDPTab, error) {
	resp, err := http.Get(c.baseURL() + "/json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	var targets []struct {
		ID                 string `json:"id"`
		Title              string `json:"title"`
		URL                string `json:"url"`
		Type               string `json:"type"`
		WebSocketDebuggerUrl string `json:"webSocketDebuggerUrl"`
	}
	if err := json.Unmarshal(body, &targets); err != nil {
		return nil, err
	}

	var tabs []*CDPTab
	for _, t := range targets {
		if t.Type == "page" {
			tabs = append(tabs, &CDPTab{
				ID:    t.ID,
				Title: t.Title,
				URL:   t.URL,
				Type:  t.Type,
				WsURL: t.WebSocketDebuggerUrl,
				pending: make(map[int64]chan json.RawMessage),
				events:  make(map[string][]func(json.RawMessage)),
			})
		}
	}
	c.tabs = tabs
	return tabs, nil
}

func (c *CDP) Version() (map[string]interface{}, error) {
	resp, err := http.Get(c.baseURL() + "/json/version")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var v map[string]interface{}
	json.Unmarshal(body, &v)
	return v, nil
}

func (c *CDP) NewTab(url string) (*CDPTab, error) {
	u := c.baseURL() + "/json/new"
	if url != "" {
		u += "?" + url
	}
	resp, err := http.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var t struct {
		ID                 string `json:"id"`
		Title              string `json:"title"`
		URL                string `json:"url"`
		Type               string `json:"type"`
		WebSocketDebuggerUrl string `json:"webSocketDebuggerUrl"`
	}
	json.Unmarshal(body, &t)
	tab := &CDPTab{
		ID:    t.ID,
		Title: t.Title,
		URL:   t.URL,
		Type:  t.Type,
		WsURL: t.WebSocketDebuggerUrl,
		pending: make(map[int64]chan json.RawMessage),
		events:  make(map[string][]func(json.RawMessage)),
	}
	return tab, nil
}

func (c *CDP) CloseTab(tabID string) error {
	resp, err := http.Get(c.baseURL() + "/json/close/" + tabID)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

// --- CDPTab methods ---

func (t *CDPTab) Connect() error {
	dialer := websocket.Dialer{
		HandshakeTimeout: 5 * time.Second,
	}
	conn, _, err := dialer.Dial(t.WsURL, nil)
	if err != nil {
		return err
	}
	t.conn = conn
	go t.readLoop()
	return nil
}

func (t *CDPTab) Close() {
	if t.conn != nil {
		t.conn.Close()
	}
}

func (t *CDPTab) readLoop() {
	for {
		_, msg, err := t.conn.ReadMessage()
		if err != nil {
			return
		}
		var envelope struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Result json.RawMessage `json:"result"`
			Params json.RawMessage `json:"params"`
			Error  json.RawMessage `json:"error"`
		}
		json.Unmarshal(msg, &envelope)

		if envelope.ID > 0 {
			t.pendingMu.Lock()
			ch, ok := t.pending[envelope.ID]
			if ok {
				delete(t.pending, envelope.ID)
			}
			t.pendingMu.Unlock()
			if ok {
				if envelope.Error != nil {
					ch <- envelope.Error
				} else {
					ch <- envelope.Result
				}
			}
		} else if envelope.Method != "" {
			t.eventsMu.RLock()
			handlers := t.events[envelope.Method]
			t.eventsMu.RUnlock()
			for _, h := range handlers {
				go h(envelope.Params)
			}
		}
	}
}

func (t *CDPTab) Send(method string, params map[string]interface{}) (json.RawMessage, error) {
	id := t.msgID.Add(1)
	ch := make(chan json.RawMessage, 1)

	t.pendingMu.Lock()
	t.pending[id] = ch
	t.pendingMu.Unlock()

	msg := map[string]interface{}{
		"id":     id,
		"method": method,
	}
	if params != nil {
		msg["params"] = params
	}

	t.mu.Lock()
	err := t.conn.WriteJSON(msg)
	t.mu.Unlock()
	if err != nil {
		return nil, err
	}

	select {
	case result := <-ch:
		return result, nil
	case <-time.After(30 * time.Second):
		t.pendingMu.Lock()
		delete(t.pending, id)
		t.pendingMu.Unlock()
		return nil, fmt.Errorf("timeout waiting for %s", method)
	}
}

func (t *CDPTab) On(method string, handler func(json.RawMessage)) {
	t.eventsMu.Lock()
	t.events[method] = append(t.events[method], handler)
	t.eventsMu.Unlock()
}

// High-level CDP methods

func (t *CDPTab) Navigate(url string) error {
	_, err := t.Send("Page.navigate", map[string]interface{}{"url": url})
	return err
}

func (t *CDPTab) Eval(expr string) (json.RawMessage, error) {
	result, err := t.Send("Runtime.evaluate", map[string]interface{}{
		"expression":    expr,
		"returnByValue": true,
	})
	if err != nil {
		return nil, err
	}
	var res struct {
		Result struct {
			Value json.RawMessage `json:"value"`
		} `json:"result"`
	}
	json.Unmarshal(result, &res)
	return res.Result.Value, nil
}

func (t *CDPTab) Screenshot() ([]byte, error) {
	b64, err := t.ScreenshotB64()
	if err != nil {
		return nil, err
	}
	return base64.StdEncoding.DecodeString(b64)
}

func (t *CDPTab) ScreenshotB64() (string, error) {
	result, err := t.Send("Page.captureScreenshot", map[string]interface{}{"format": "png"})
	if err != nil {
		return "", err
	}
	var res struct {
		Data string `json:"data"`
	}
	json.Unmarshal(result, &res)
	return res.Data, nil
}

func (t *CDPTab) GetTitle() (string, error) {
	v, err := t.Eval("document.title")
	if err != nil {
		return "", err
	}
	var s string
	json.Unmarshal(v, &s)
	return s, nil
}

func (t *CDPTab) GetURL() (string, error) {
	v, err := t.Eval("location.href")
	if err != nil {
		return "", err
	}
	var s string
	json.Unmarshal(v, &s)
	return s, nil
}

// CDP Input domain — virtual events, zero physical interference

func (t *CDPTab) Click(x, y float64) error {
	t.Send("Input.dispatchMouseEvent", map[string]interface{}{
		"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
	})
	_, err := t.Send("Input.dispatchMouseEvent", map[string]interface{}{
		"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
	})
	return err
}

func (t *CDPTab) TypeCDP(text string) error {
	for _, ch := range text {
		t.Send("Input.dispatchKeyEvent", map[string]interface{}{
			"type": "keyDown", "text": string(ch),
		})
		t.Send("Input.dispatchKeyEvent", map[string]interface{}{
			"type": "keyUp",
		})
	}
	return nil
}

func (t *CDPTab) Scroll(x, y, dx, dy float64) error {
	_, err := t.Send("Input.dispatchMouseEvent", map[string]interface{}{
		"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy,
	})
	return err
}

// --- Auto-discovery ---

func FindChromeDebugPort() int {
	ports := []int{9222, 9229, 9223, 9224, 9225}
	for _, port := range ports {
		url := fmt.Sprintf("http://127.0.0.1:%d/json/version", port)
		client := &http.Client{Timeout: 500 * time.Millisecond}
		resp, err := client.Get(url)
		if err == nil {
			resp.Body.Close()
			return port
		}
	}
	return 0
}

// --- Utility ---

func parsePort(s string) int {
	p, _ := strconv.Atoi(s)
	return p
}

func jsonStr(v interface{}) string {
	b, _ := json.MarshalIndent(v, "", "  ")
	return string(b)
}

func jsonCompact(v interface{}) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func contains(s, sub string) bool {
	return strings.Contains(strings.ToLower(s), strings.ToLower(sub))
}
