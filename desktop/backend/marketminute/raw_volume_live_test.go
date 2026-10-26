package marketminute

import (
	"encoding/json"
	"os"
	"testing"
	"time"

	gotdx "github.com/bensema/gotdx"
	"github.com/bensema/gotdx/types"
)

// Explicit, read-only provider probe; ordinary tests never access live data.
func TestMACRawVolumeLiveProbe(t *testing.T) {
	output := os.Getenv("STOCK_KING_RAW_VOLUME_EVIDENCE")
	if output == "" {
		t.Skip("opt-in raw public MAC volume evidence")
	}
	client := gotdx.NewMAC(gotdx.WithTimeoutSec(8))
	defer client.Disconnect()
	if err := client.ConnectMAC(); err != nil {
		t.Fatal(err)
	}
	targets := map[string]bool{"2026-10-13 14:11:00": true, "2026-10-13 11:09:00": true, "2026-10-16 13:54:00": true}
	rows := []map[string]any{}
	var offset uint32
	pages := 0
	for ; pages < 8 && len(rows) < len(targets); pages++ {
		reply, err := client.GetMACSymbolBars(0, "002980", types.KLINE_TYPE_1MIN, 1, offset, 700, types.AdjustNone)
		if err != nil {
			t.Fatal(err)
		}
		if err = ValidateReply(reply, 0, "002980", types.KLINE_TYPE_1MIN, offset); err != nil {
			t.Fatal(err)
		}
		if len(reply.List) == 0 {
			break
		}
		for _, bar := range reply.List {
			if targets[bar.DateTime.Format("2006-01-02 15:04:05")] {
				rows = append(rows, map[string]any{"sourceTime": bar.DateTime.Format(time.RFC3339),
					"rawVol": bar.Vol, "rawAmount": bar.Amount, "rawOpen": bar.Open,
					"rawHigh": bar.High, "rawLow": bar.Low, "rawClose": bar.Close,
					"responseVolUnit": reply.VolUnit, "responseCode": reply.Code,
					"responseMarket": reply.Market, "pageOffset": offset})
			}
		}
		offset += uint32(len(reply.List))
	}
	packet := map[string]any{"observedAt": time.Now().Format(time.RFC3339), "pages": pages,
		"code": "002980", "method": "direct GetMACSymbolBars before all application adapters; IEEE754 Float32 decoding", "rows": rows}
	data, err := json.MarshalIndent(packet, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(output, data, 0600); err != nil {
		t.Fatal(err)
	}
	t.Log(string(data))
	if len(rows) != len(targets) {
		t.Fatalf("received %d/%d requested historical observations", len(rows), len(targets))
	}
}

func TestClassicRawVolumeLiveProbe(t *testing.T) {
	output := os.Getenv("STOCK_KING_CLASSIC_VOLUME_EVIDENCE")
	if output == "" {
		t.Skip("opt-in raw public classic TDX volume evidence")
	}
	client := gotdx.New(gotdx.WithAutoSelectFastest(true), gotdx.WithTimeoutSec(8))
	defer client.Disconnect()
	if _, err := client.Connect(); err != nil {
		t.Fatal(err)
	}
	targets := map[string]bool{"2026-10-13 14:11:00": true, "2026-10-13 11:09:00": true, "2026-10-16 13:54:00": true}
	rows := []map[string]any{}
	var offset uint16
	pages := 0
	for ; pages < 8 && len(rows) < len(targets); pages++ {
		reply, err := client.GetSecurityBars(types.KLINE_TYPE_1MIN, 0, "002980", offset, 700)
		if err != nil {
			t.Fatal(err)
		}
		if len(reply.List) == 0 {
			break
		}
		for _, bar := range reply.List {
			if targets[bar.DateTime.Format("2006-01-02 15:04:05")] {
				rows = append(rows, map[string]any{"sourceTime": bar.DateTime.Format(time.RFC3339),
					"rawVol": bar.Vol, "rawAmount": bar.Amount, "rawOpen": bar.Open,
					"rawHigh": bar.High, "rawLow": bar.Low, "rawClose": bar.Close,
					"pageOffset": offset})
			}
		}
		offset += uint16(len(reply.List))
	}
	packet := map[string]any{"observedAt": time.Now().Format(time.RFC3339), "pages": pages,
		"code": "002980", "method": "direct classic GetSecurityBars before application adapters", "rows": rows}
	data, err := json.MarshalIndent(packet, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(output, data, 0600); err != nil {
		t.Fatal(err)
	}
	t.Log(string(data))
	if len(rows) != len(targets) {
		t.Fatalf("received %d/%d requested historical observations", len(rows), len(targets))
	}
}
