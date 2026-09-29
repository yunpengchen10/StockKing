package main

import (
	"go-stock/backend/data"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestMarketGatewayRejectsUnauthenticatedAndRemote(t *testing.T) {
	for _, tc := range []struct{ remote, token string }{{"127.0.0.1:1000", ""}, {"10.0.0.1:1000", "secret"}} {
		request := httptest.NewRequest("POST", "/v1/bars", strings.NewReader(`{"codes":[]}`))
		request.RemoteAddr = tc.remote
		request.Header.Set("X-Stock-King-Market-Token", tc.token)
		response := httptest.NewRecorder()
		marketGatewayHandler("secret").ServeHTTP(response, request)
		if response.Code != 401 {
			t.Fatalf("expected401 got%d", response.Code)
		}
	}
}

func TestMarketGatewayRejectsInvalidSymbols(t *testing.T) {
	request := httptest.NewRequest("POST", "/v1/bars", strings.NewReader(`{"codes":["../../secret"]}`))
	request.RemoteAddr = "127.0.0.1:1000"
	request.Header.Set("X-Stock-King-Market-Token", "secret")
	response := httptest.NewRecorder()
	marketGatewayHandler("secret").ServeHTTP(response, request)
	if response.Code != 400 {
		t.Fatalf("expected400 got%d", response.Code)
	}
}

func TestMarketGatewayCompletedMinutesAndMissingAmount(t *testing.T) {
	rows := []data.KLineData{
		{Day: "2026-09-29 09:40", Open: "10", High: "10.2", Low: "9.9", Close: "10.1", Volume: "100", Amount: ""},
		{Day: "2026-09-29 09:41", Open: "10.1", High: "10.2", Low: "10", Close: "10.2", Volume: "500", Amount: "5100"},
	}
	raw := &data.KLineSourceResult{Data: &rows, Source: "sina", VolumeUnit: "shares", AmountUnit: "CNY", Adjustment: "none"}
	result := normalizedGatewayBars(raw, "1", time.Date(2026, 9, 29, 9, 40, 30, 0, shanghaiMarketTZ))
	bars := result["bars"].([]map[string]any)
	if len(bars) != 1 || bars[0]["amount_cny"] != nil || bars[0]["volume_shares"] != float64(100) || bars[0]["close"] != float64(10.1) {
		t.Fatalf("wrong normalized bars: %#v", bars)
	}
	if bars[0]["end"] != "2026-09-29T09:40:00+08:00" {
		t.Fatal(bars[0]["end"])
	}
}

func TestMarketGatewayDoesNotAgePartialCachedBarIntoCompletion(t *testing.T) {
	rows := []data.KLineData{{Day: "2026-09-29 09:40", Open: "10", High: "11", Low: "10", Close: "11", Volume: "100", Amount: "1050"}}
	raw := &data.KLineSourceResult{Data: &rows, Source: "tdx-mac", FetchedAt: "2026-09-29T09:39:55+08:00"}
	result := normalizedGatewayBars(raw, "1", time.Date(2026, 9, 29, 9, 40, 1, 0, shanghaiMarketTZ))
	if len(result["bars"].([]map[string]any)) != 0 {
		t.Fatal("cached partial minute accepted as complete")
	}
}
