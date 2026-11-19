package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestLocalPicksRefreshUsesIndependentAsyncRoute(t *testing.T) {
	var calls []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/stock-king/picks/refresh":
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Error(err)
			}
			if body["max_per_board"] != float64(5) || body["scan_slot"] != "live" || body["official"] != false {
				t.Errorf("unexpected scan request: %#v", body)
			}
			_, _ = w.Write([]byte(`{"task_id":"refresh-1","status":"pending"}`))
		case "/api/v1/stock-king/picks/refresh/tasks/refresh-1":
			_, _ = w.Write([]byte(`{"status":"completed","result":{"adaptive":{"candidates":[]}}}`))
		case "/api/v1/stock-king/picks/display":
			_, _ = w.Write([]byte(`{"adaptive":{"generatedAt":"2026-09-29T09:20:00+08:00","candidates":[{"code":"600000"}]}}`))
		default:
			t.Errorf("local picks must not call classic or history scan routes: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	app := &App{ctx: context.Background(), sidecar: &SidecarManager{
		port:   server.Listener.Addr().(*net.TCPAddr).Port,
		status: EngineStatus{Ready: true}, client: server.Client(),
	}}
	started, err := app.StartKingPicksRefresh(0)
	if err != nil || started["task_id"] != "refresh-1" {
		t.Fatalf("start: %#v, %v", started, err)
	}
	completed, err := app.GetKingPicksRefreshTask(" refresh-1 ")
	if err != nil || completed["status"] != "completed" {
		t.Fatalf("poll: %#v, %v", completed, err)
	}
	saved, err := app.GetDisplayedKingPicks()
	if err != nil || saved["adaptive"] == nil {
		t.Fatalf("restore: %#v, %v", saved, err)
	}
	if len(calls) != 3 {
		t.Fatalf("unexpected calls: %v", calls)
	}
}

func TestLocalPicksControlHonorsShutdown(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	app := &App{ctx: ctx, sidecar: &SidecarManager{status: EngineStatus{Ready: true}, client: &http.Client{Timeout: time.Minute}}}
	if _, err := app.GetDisplayedKingPicks(); err == nil {
		t.Fatal("expected cancelled request")
	}
	if _, err := app.GetKingPicksRefreshTask(" "); err == nil {
		t.Fatal("empty task ID must fail without a request")
	}
}

func TestStrategyRefreshAndImportRoutesUseExplicitSelection(t *testing.T) {
	var calls []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodPost {
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			switch r.URL.Path {
			case "/api/v1/stock-king/picks/refresh":
				if body["strategy_id"] != "theme_lead" || body["top_n"] != float64(5) {
					t.Errorf("selection lost: %#v", body)
				}
			case "/api/v1/stock-king/picks/strategies/import":
				if body["id"] != "custom.mine" {
					t.Errorf("definition lost: %#v", body)
				}
			case "/api/v1/stock-king/picks/strategies/preview":
				if body["strategy_id"] != "custom.mine" || body["run_id"] != "frozen-run" {
					t.Errorf("preview source lost: %#v", body)
				}
			case "/api/v1/stock-king/picks/strategies/custom.mine/enable":
				if body["confirm"] != true {
					t.Errorf("confirmation missing: %#v", body)
				}
				if body["enabled"] == false && (body["expected_version"] != "2" || body["expected_fingerprint"] != "reviewed-definition") {
					t.Errorf("version-bound disable missing: %#v", body)
				}
			default:
				t.Errorf("unexpected mutation: %s", r.URL.Path)
			}
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()
	app := &App{ctx: context.Background(), sidecar: &SidecarManager{port: server.Listener.Addr().(*net.TCPAddr).Port, status: EngineStatus{Ready: true}, client: server.Client()}}
	if _, err := app.StartKingPicksStrategyRefresh(5, "theme_lead"); err != nil {
		t.Fatal(err)
	}
	if _, err := app.GetKingPicksStrategies(); err != nil {
		t.Fatal(err)
	}
	if _, err := app.ImportKingPicksStrategy(map[string]any{"id": "custom.mine"}); err != nil {
		t.Fatal(err)
	}
	if _, err := app.PreviewKingPicksStrategy("custom.mine", "frozen-run"); err != nil {
		t.Fatal(err)
	}
	if _, err := app.EnableKingPicksStrategy("custom.mine"); err != nil {
		t.Fatal(err)
	}
	if _, err := app.SetKingPicksStrategyEnabled("custom.mine", false, "2", "reviewed-definition"); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 6 {
		t.Fatalf("unexpected calls: %v", calls)
	}
}
