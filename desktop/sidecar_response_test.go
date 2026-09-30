package main

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newSidecarResponseTestApp(t *testing.T, handler http.HandlerFunc) *App {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return &App{ctx: context.Background(), sidecar: &SidecarManager{
		port:   server.Listener.Addr().(*net.TCPAddr).Port,
		status: EngineStatus{Ready: true}, client: server.Client(),
	}}
}

func TestSidecarRecommendationHistoryReadsBeyond16MiB(t *testing.T) {
	// Saved snapshots contain both ASCII fields and UTF-8 evidence. The final
	// record must survive even when the response exceeds the former read limit.
	const chunk = "candidate snapshot 精选证据; "
	snapshot := strings.Repeat(chunk, (16<<20)/len(chunk)+1)
	response, err := json.Marshal(map[string]any{"items": []any{
		map[string]any{"signalId": "first", "code": "600000", "selected": true, "snapshot": snapshot},
		map[string]any{"signalId": "last-sentinel", "code": "000001", "name": "末尾记录", "selected": true, "signalPrice": 10.12},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if len(response) <= 16<<20 {
		t.Fatalf("fixture must exceed 16 MiB; got %d bytes", len(response))
	}
	app := newSidecarResponseTestApp(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/v1/stock-king/picks/records" {
			t.Errorf("unexpected history request: %s %s", r.Method, r.URL.Path)
		}
		if r.URL.Query().Get("date") != "2026-09-30" || r.URL.Query().Get("symbol") != "600000" || r.URL.Query().Get("version") != "rules-v1" {
			t.Errorf("history filters were not preserved: %s", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(response)
	})
	result, err := app.GetStockKingRecommendationHistory("2026-09-30", "600000", "rules-v1")
	if err != nil {
		t.Fatalf("reading %d-byte history: %v", len(response), err)
	}
	items, ok := result["items"].([]any)
	if !ok || len(items) != 2 {
		t.Fatalf("expected both history records, got %d", len(items))
	}
	first := items[0].(map[string]any)
	if first["snapshot"] != snapshot {
		t.Fatal("the saved UTF-8 snapshot was truncated or altered")
	}
	last := items[1].(map[string]any)
	if last["signalId"] != "last-sentinel" || last["name"] != "末尾记录" || last["signalPrice"] != 10.12 {
		t.Fatalf("the final history record was lost or altered: %#v", last)
	}
}

func TestSidecarRecommendationHistoryRejectsTruncatedJSON(t *testing.T) {
	app := newSidecarResponseTestApp(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[{"signalId":"unfinished"`))
	})
	result, err := app.GetStockKingRecommendationHistory("", "", "")
	var syntaxError *json.SyntaxError
	if err == nil || !errors.As(err, &syntaxError) {
		t.Fatalf("expected a JSON syntax error for an incomplete response, got %v", err)
	}
	if result != nil {
		t.Fatal("incomplete history must not be returned as valid records")
	}
}

func TestSidecarErrorResponsePreservesStatusWithBoundedDetails(t *testing.T) {
	const detailLimit = 64 << 10
	const prefix = "history temporarily unavailable: "
	const tail = "END-OF-OVERSIZED-ERROR"
	app := newSidecarResponseTestApp(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(prefix + strings.Repeat("x", 2*detailLimit) + tail))
	})
	_, err := app.GetStockKingRecommendationHistory("", "", "")
	if err == nil {
		t.Fatal("an unsuccessful HTTP response must return an error")
	}
	message := err.Error()
	for _, expected := range []string{"GET", "/api/v1/stock-king/picks/records", "503", prefix} {
		if !strings.Contains(message, expected) {
			t.Fatalf("HTTP error is missing %q", expected)
		}
	}
	if len(message) > detailLimit+512 || strings.Contains(message, tail) {
		t.Fatalf("HTTP error details were not bounded: got %d bytes", len(message))
	}
}

func TestSidecarEmptySuccessfulResponseRemainsAllowed(t *testing.T) {
	for _, status := range []int{http.StatusOK, http.StatusNoContent} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			app := newSidecarResponseTestApp(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
			})
			result, err := app.GetStockKingRecommendationHistory("", "", "")
			if err != nil || result != nil {
				t.Fatalf("empty successful response changed behavior: result=%#v, error=%v", result, err)
			}
		})
	}
}
