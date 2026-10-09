// Export public unadjusted minute evidence. This command never opens an app database.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	gotdx "github.com/bensema/gotdx"
	"github.com/bensema/gotdx/types"
	"go-stock/backend/marketminute"
)

func main() {
	codesFlag := flag.String("codes", "", "Comma-separated six-digit A-share codes")
	codesFile := flag.String("codes-file", "", "Text or JSON list containing six-digit codes")
	output := flag.String("output-dir", "", "Evidence output directory (required)")
	count := flag.Int("count", 8000, "Maximum unique bars per stock")
	end := flag.String("end", "", "Optional historical cutoff YYYYMMDDHHMMSS")
	workers := flag.Int("workers", 3, "Connections, from 1 to 4")
	budget := flag.Duration("timeout", 10*time.Minute, "Overall queue deadline")
	flag.Parse()
	if *output == "" || *workers < 1 || *workers > 4 || *count < 1 || *count > 32000 {
		fmt.Fprintln(os.Stderr, "invalid output, workers or count")
		os.Exit(2)
	}
	raw := *codesFlag
	if *codesFile != "" {
		b, err := os.ReadFile(*codesFile)
		if err != nil {
			panic(err)
		}
		raw += "," + string(b)
	}
	seen := map[string]bool{}
	codes := []string{}
	for _, code := range regexp.MustCompile(`\b(?:60|68|00|30)\d{4}\b`).FindAllString(raw, -1) {
		if !seen[code] {
			seen[code] = true
			codes = append(codes, code)
		}
	}
	if len(codes) == 0 {
		fmt.Fprintln(os.Stderr, "no codes")
		os.Exit(2)
	}
	if err := os.MkdirAll(*output, 0755); err != nil {
		panic(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), *budget)
	defer cancel()
	queue := make(chan string, len(codes))
	for _, code := range codes {
		queue <- code
	}
	close(queue)
	var wg sync.WaitGroup
	var mu sync.Mutex
	receipts := []map[string]any{}
	for worker := 0; worker < *workers; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			client := gotdx.NewMAC(gotdx.WithAutoSelectFastest(true), gotdx.WithTimeoutSec(4))
			defer client.Disconnect()
			for code := range queue {
				if ctx.Err() != nil {
					break
				}
				market := uint8(types.MarketSZ)
				if strings.HasPrefix(code, "6") {
					market = uint8(types.MarketSH)
				}
				started := time.Now()
				rows, err := marketminute.FetchContext(ctx, client, market, code, uint16(types.KLINE_TYPE_1MIN), *count, types.AdjustNone, *end)
				fetched := time.Now()
				stamp := fetched.Format(time.RFC3339Nano)
				receipt := map[string]any{"code": code, "requestedCount": *count, "requestStartedAt": started.Format(time.RFC3339Nano), "fetchedAt": stamp, "elapsedSeconds": time.Since(started).Seconds()}
				if err != nil {
					receipt["error"] = err.Error()
				} else {
					bars := []map[string]any{}
					days := map[string]int{}
					for _, row := range rows {
						clock := row.DateTime.Format("15:04")
						if row.DateTime.After(started) || !(clock >= "09:31" && clock <= "11:30" || clock >= "13:01" && clock <= "15:00") {
							continue
						}
						price := func(value float64) float64 { return math.Round(value*100) / 100 }
						bar := map[string]any{"end": row.DateTime.Format(time.RFC3339), "open": price(row.Open), "high": price(row.High), "low": price(row.Low), "close": price(row.Close), "amount_cny": price(row.Amount), "volume_shares": math.Round(row.Vol), "source": "tdx-mac 1min unadjusted", "fetched_at": stamp}
						bars = append(bars, bar)
						days[row.DateTime.Format("2006-01-02")]++
					}
					packet := map[string]any{"code": code, "source": "tdx-mac 1min unadjusted", "requestStartedAt": started.Format(time.RFC3339Nano), "fetchedAt": stamp, "requestedCount": *count, "requestedEnd": *end, "rawCount": len(rows), "normalized_bars": bars, "byDay": days, "readOnlySource": true}
					path := filepath.Join(*output, "mac-"+code+".json")
					encoded, e := json.Marshal(packet)
					if e == nil {
						e = os.WriteFile(path, encoded, 0644)
					}
					if e != nil {
						receipt["error"] = e.Error()
					} else {
						receipt["artifact"] = path
						receipt["bars"] = len(bars)
						receipt["days"] = len(days)
						receipt["rawCount"] = len(rows)
					}
				}
				mu.Lock()
				receipts = append(receipts, receipt)
				encoded, _ := json.Marshal(receipt)
				fmt.Println(string(encoded))
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	manifest := map[string]any{"requested": len(codes), "finished": len(receipts), "deferred": len(codes) - len(receipts), "items": receipts, "completedAt": time.Now().Format(time.RFC3339Nano)}
	encoded, _ := json.MarshalIndent(manifest, "", "  ")
	if err := os.WriteFile(filepath.Join(*output, "mac-manifest.json"), encoded, 0644); err != nil {
		panic(err)
	}
}
