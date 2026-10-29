package main

import (
	"context"
	"io"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

type recordReadTransport func(*http.Request) (*http.Response, error)

func (f recordReadTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestRecommendationReadIsCompactAndHasShortDeadline(t *testing.T) {
	for _, includeControls := range []bool{false, true} {
		app := &App{ctx: context.Background(), sidecar: &SidecarManager{
			status: EngineStatus{Ready: true}, port: 12345, token: "test-token",
			client: &http.Client{Transport: recordReadTransport(func(request *http.Request) (*http.Response, error) {
				query := request.URL.Query()
				if query.Get("compact") != "true" || query.Get("include_controls") != map[bool]string{false: "false", true: "true"}[includeControls] {
					t.Fatalf("invalid record query: %v", query)
				}
				if query.Get("version") != "v1.2 + fixed" || query.Get("symbol") != "600001" || query.Get("date") != "2026-10-12" || query.Get("run_id") != "king-live-test" {
					t.Fatalf("filters changed: %v", query)
				}
				deadline, ok := request.Context().Deadline()
				if !ok || time.Until(deadline) > 15*time.Second || time.Until(deadline) < 14*time.Second {
					t.Fatal("record read must have a 15 second deadline")
				}
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"items":[],"count":0}`))}, nil
			})},
		}}
		result, err := app.GetStockKingRecommendationHistory("2026-10-12", "600001", "v1.2 + fixed", includeControls, "king-live-test")
		if err != nil || result["count"] != float64(0) {
			t.Fatalf("read failed: %v %v", result, err)
		}
	}
}

func TestRecordReadsRespectParentCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	app := &App{ctx: ctx, sidecar: &SidecarManager{
		status: EngineStatus{Ready: true}, port: 12345,
		client: &http.Client{Transport: recordReadTransport(func(request *http.Request) (*http.Response, error) {
			if request.Context().Err() != context.Canceled {
				t.Fatal("record read discarded shutdown cancellation")
			}
			return nil, request.Context().Err()
		})},
	}}
	if _, err := app.GetStockKingDelayedReviews("", "", ""); err == nil {
		t.Fatal("cancelled review read should return an error")
	} else if _, ok := err.(*url.Error); !ok {
		t.Fatalf("unexpected transport error: %T", err)
	}
}
