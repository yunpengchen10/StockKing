package main

import (
	"go-stock/backend/data"
	"testing"
	"time"
)

func TestGatewayPreservesCheckedSecurityIdentity(t *testing.T) {
	rows := []data.KLineData{{Day: "2026-10-08 09:31", Open: "4.50", High: "4.51", Low: "4.49", Close: "4.50", Volume: "100", Amount: "450"}}
	raw := &data.KLineSourceResult{Data: &rows, Source: "tdx-mac"}
	packet := normalizedGatewayBars(raw, "1", time.Date(2026, 10, 8, 10, 0, 0, 0, shanghaiMarketTZ), "000692")
	identity := packet["identity_evidence"].(map[string]any)
	if identity["status"] != "provider_reply_checked" || identity["response_code"] != "000692" || identity["requested_market"] != uint8(0) {
		t.Fatalf("lost identity evidence: %+v", identity)
	}
	bar := packet["bars"].([]map[string]any)[0]
	if bar["identity_evidence"].(map[string]any)["response_code"] != "000692" {
		t.Fatal("archive bar has no checked security identity")
	}
	raw.Source = "sina"
	packet = normalizedGatewayBars(raw, "1", time.Now(), "000692")
	if packet["identity_evidence"].(map[string]any)["status"] != "request_only" {
		t.Fatal("fallback source was falsely assigned a verified provider echo")
	}
}
