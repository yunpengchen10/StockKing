package main

import (
	"context"
	"fmt"
	"go-stock/backend/data"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func recordTencentFixture(symbol, price, stamp string) string {
	parts := make([]string, 38)
	parts[1], parts[2], parts[3], parts[4], parts[30] = "股票", symbol[2:], price, "99", stamp
	return "v_" + symbol + "=\"" + strings.Join(parts, "~") + "\";"
}

func recordSinaFixture(symbol, price, day, clock string) string {
	parts := make([]string, 33)
	parts[0], parts[2], parts[3], parts[30], parts[31] = "股票", "99", price, day, clock
	return "var hq_str_" + symbol + "=\"" + strings.Join(parts, ",") + "\";"
}

func TestRecordQuotesValidateEvidence(t *testing.T) {
	now := time.Date(2026, 9, 30, 10, 0, 0, 0, recordQuoteZone)
	for _, tc := range []struct{ source, body, want string }{
		{"tencent", recordTencentFixture("sh600000", "12.34", "20260930100000"), "2026-09-30T10:00:00+08:00"},
		{"sina", recordSinaFixture("sh600000", "12.34", "2026-09-29", "15:00:00"), "2026-09-29T15:00:00+08:00"},
		{"tencent", recordTencentFixture("sh600000", "0", "20260930100000"), ""},
		{"tencent", recordTencentFixture("sh600000", "NaN", "20260930100000"), ""},
		{"sina", recordSinaFixture("sh600000", "-1", "2026-09-30", "10:00:00"), ""},
		{"sina", recordSinaFixture("sh600000", "+Inf", "2026-09-30", "10:00:00"), ""},
		{"tencent", recordTencentFixture("sh600000", "12.34", "20260930110000"), ""},
		{"tencent", recordTencentFixture("sh600000", "12.34", "invalid"), ""},
		{"tencent", recordTencentFixture("sh600001", "12.34", "20260930100000"), ""},
		{"tencent", `v_sh600000="short";`, ""},
	} {
		quotes := parseRecordQuotes(tc.source, tc.body, "received-time", []string{"sh600000"}, now)
		if tc.want == "" {
			if len(quotes) != 0 {
				t.Errorf("invalid quote accepted: %s %#v", tc.body, quotes)
			}
			continue
		}
		quote := quotes["600000"]
		if quote.Price != 12.34 || quote.SourceTime != tc.want || quote.FetchedAt != "received-time" || quote.Source != tc.source {
			t.Errorf("provider evidence changed: %#v", quote)
		}
	}
}

func TestRecordQuotesDeduplicateAndFallbackOnlyMissing(t *testing.T) {
	var calls []string
	result, err := fetchRecordQuotes(context.Background(), []string{"600000", "sh600000", "600000.SH", "000001", "sh000001", "600002&evil", ""},
		func(ctx context.Context, source string, symbols []string) (string, string, error) {
			if _, ok := ctx.Deadline(); !ok {
				t.Error("provider request has no deadline")
			}
			calls = append(calls, source+":"+strings.Join(symbols, ","))
			if source == "tencent" {
				return recordTencentFixture("sh600000", "12.34", "20260929150000") + recordTencentFixture("sz000001", "0", "20260929150000"), "received", nil
			}
			return recordSinaFixture("sz000001", "10", "2026-09-29", "15:00:00"), "received", nil
		})
	if err != nil || len(result["quotes"].(map[string]recordQuote)) != 2 || len(result["errors"].([]string)) != 0 {
		t.Fatalf("unexpected result: %#v %v", result, err)
	}
	if !reflect.DeepEqual(calls, []string{"tencent:sz000001,sh600000", "sina:sz000001"}) {
		t.Fatalf("unexpected requests: %v", calls)
	}
}

func TestRecordQuotesBoundParallelRequestsAndReportPartialFailures(t *testing.T) {
	codes := make([]string, 125)
	for i := range codes {
		codes[i] = fmt.Sprintf("%06d", 600000+i)
	}
	var mutex sync.Mutex
	active, maximum := 0, 0
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	started := time.Now()
	result, err := fetchRecordQuotes(ctx, codes, func(ctx context.Context, source string, symbols []string) (string, string, error) {
		if len(symbols) > 30 {
			t.Errorf("oversized batch: %d", len(symbols))
		}
		mutex.Lock()
		active++
		maximum = max(maximum, active)
		mutex.Unlock()
		<-ctx.Done()
		mutex.Lock()
		active--
		mutex.Unlock()
		return "", "", ctx.Err()
	})
	if err != nil || maximum > 4 || maximum == 0 || time.Since(started) > time.Second || len(result["errors"].([]string)) != 1 {
		t.Fatalf("request budget/feedback: max=%d result=%#v err=%v elapsed=%s", maximum, result, err, time.Since(started))
	}
}

func TestRecordQuotesLiveSmoke(t *testing.T) {
	if os.Getenv("STOCK_KING_LIVE_QUOTES_TEST") != "1" {
		t.Skip("opt-in provider integration smoke")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
	defer cancel()
	started := time.Now()
	result, err := fetchRecordQuotes(ctx, []string{"600000", "000001"}, data.FetchSharedQuotePayloadContext)
	if err != nil || len(result["quotes"].(map[string]recordQuote)) != 2 {
		t.Fatalf("live provider quotes: %#v %v", result, err)
	}
	t.Logf("live quotes: %#v, elapsed=%s", result, time.Since(started))
}
