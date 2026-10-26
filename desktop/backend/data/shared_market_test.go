package data

import (
	"encoding/json"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/bensema/gotdx/proto"
)

func TestMACFractionalLotsSurviveShareNormalization(t *testing.T) {
	rows := convertMACSymbolBar([]proto.MACSymbolBar{{
		Open: 116.05, High: 116.05, Low: 116.05, Close: 116.05,
		Vol: 12.8, Amount: 148544,
	}})
	if rows[0].Volume != "12.8" {
		t.Fatalf("fractional lots were rounded before unit conversion: %s", rows[0].Volume)
	}
	normalizeSharedVolume(&KLineSourceResult{Data: &rows, Source: "tdx-mac"}, "1")
	if rows[0].Volume != "1280" || rows[0].Amount != "148544.00" {
		t.Fatalf("odd-lot volume/amount corrupted: %+v", rows[0])
	}
}

func TestSharedMarketNormalizesUnitsAndPreservesUnknownAmount(t *testing.T) {
	for _, source := range []string{"tdx-mac", "eastmoney", "sina", "tencent"} {
		rows := []KLineData{{Low: "9", High: "11", Volume: "100", Amount: "100000"}, {Low: "9", High: "11", Volume: "10000", Amount: "100000"}, {Low: "9", High: "11", Volume: "100", Amount: "700000000"}}
		normalizeSharedVolume(&KLineSourceResult{Data: &rows, Source: source}, "1")
		if rows[0].Volume != "10000" || rows[1].Volume != "10000" || rows[2].Volume != "" || rows[2].Amount != "" {
			t.Fatalf("%s: %+v", source, rows)
		}
	}
	var item SinaKLineItem
	if err := json.Unmarshal([]byte(`{"day":"2026-09-29 09:40:00","close":"10","volume":500}`), &item); err != nil {
		t.Fatal(err)
	}
	api := &SinaKLineApi{}
	rows := api.convertToKLineData([]SinaKLineItem{item}, "1")
	if rows[0].Amount != "" {
		t.Fatal("unknown amount became zero")
	}
}

func TestSharedKLinesCoalesceRequestsAndIsolateRows(t *testing.T) {
	var calls atomic.Int32
	started, release := make(chan struct{}), make(chan struct{})
	fetch := func(string, string, string, int, string, ...string) *KLineSourceResult {
		if calls.Add(1) == 1 {
			close(started)
		}
		<-release
		rows := []KLineData{{Day: "2026-10-13", Close: "10", Volume: "100"}}
		return &KLineSourceResult{Data: &rows, Source: "tdx-mac"}
	}
	const callers = 24
	results := make([]*KLineSourceResult, callers)
	var wg sync.WaitGroup
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i] = fetchSharedKLines("600001", "", "101", 100, t.Name(), fetch, "qfq")
		}(i)
	}
	<-started
	close(release)
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatalf("overlapping requests fetched %d times", calls.Load())
	}
	(*results[0].Data)[0].Close = "changed"
	for _, result := range results[1:] {
		if (*result.Data)[0].Close != "10" || result.Source != "tdx-mac" || result.FetchedAt == "" {
			t.Fatalf("caller data was shared or metadata lost: %+v", result)
		}
	}
}

func TestSharedKLinesRespectCacheScopeExpiryAndEmptyRetry(t *testing.T) {
	var calls int
	fetch := func(string, string, string, int, string, ...string) *KLineSourceResult {
		calls++
		rows := []KLineData{{Day: "2026-10-12", Close: "9"}, {Day: "2026-10-13", Close: "10"}}
		return &KLineSourceResult{Data: &rows, Source: "tdx-mac"}
	}
	end := t.Name()
	load := func(count int, end, flag string) *KLineSourceResult {
		return fetchSharedKLines("600002", "", "101", count, end, fetch, flag)
	}
	load(2, end, "qfq")
	small := load(1, end, "qfq")
	if calls != 1 || len(*small.Data) != 1 || (*small.Data)[0].Close != "10" {
		t.Fatal("smaller chart should reuse the latest cached rows")
	}
	load(3, end, "qfq")
	load(3, end, "hfq")
	load(3, end+"other", "qfq")
	if calls != 4 {
		t.Fatalf("count, adjustment and page must remain separate, calls=%d", calls)
	}
	key := "sh600002|101|" + end + "|qfq"
	sharedBars.Lock()
	item := sharedBars.entries[key]
	item.expires = time.Now().Add(-time.Second)
	sharedBars.entries[key] = item
	sharedBars.Unlock()
	load(3, end, "qfq")
	if calls != 5 {
		t.Fatal("expired chart must refresh")
	}
	emptyCalls := 0
	empty := func(string, string, string, int, string, ...string) *KLineSourceResult {
		emptyCalls++
		return nil
	}
	for i := 0; i < 2; i++ {
		result := fetchSharedKLines("600003", "", "101", 2, end, empty, "qfq")
		if result.Data == nil || len(*result.Data) != 0 {
			t.Fatal("empty result should remain empty")
		}
	}
	if emptyCalls != 2 {
		t.Fatal("failed data fetch must not poison the cache")
	}
}
