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
