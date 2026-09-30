package main

import (
	"context"
	"fmt"
	"go-stock/backend/data"
	"math"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type recordQuote struct {
	Code       string  `json:"code"`
	Price      float64 `json:"price"`
	SourceTime string  `json:"sourceTime"`
	Source     string  `json:"source"`
	FetchedAt  string  `json:"fetchedAt"`
}

var recordCodePattern = regexp.MustCompile(`^(sh|sz)?([036][0-9]{5})(\.(sh|sz))?$`)
var recordQuoteZone = time.FixedZone("Asia/Shanghai", 8*60*60)

func recordQuoteCode(raw string) string {
	parts := recordCodePattern.FindStringSubmatch(strings.ToLower(strings.TrimSpace(raw)))
	if parts == nil {
		return ""
	}
	exchange := "sz"
	if parts[2][0] == '6' {
		exchange = "sh"
	}
	if (parts[1] != "" && parts[1] != exchange) || (parts[4] != "" && parts[4] != exchange) {
		return ""
	}
	return parts[2]
}

func recordQuoteSymbol(code string) string {
	if code[0] == '6' {
		return "sh" + code
	}
	return "sz" + code
}

// Recommendation prices are immutable ledger evidence. This reads quotes only;
// it never reruns selection, writes the ledger, or substitutes yesterday's close.
func (a *App) GetStockKingRecordQuotes(codes []string) (map[string]any, error) {
	parent := a.ctx
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, 12*time.Second)
	defer cancel()
	return fetchRecordQuotes(ctx, codes, data.FetchSharedQuotePayloadContext)
}

type recordQuoteFetcher func(context.Context, string, []string) (string, string, error)

func fetchRecordQuotes(ctx context.Context, requested []string, fetch recordQuoteFetcher) (map[string]any, error) {
	unique := make(map[string]bool)
	for _, raw := range requested {
		if code := recordQuoteCode(raw); code != "" {
			unique[code] = true
		}
	}
	if len(unique) > 2000 {
		return nil, fmt.Errorf("单次最多查询 2000 只股票，请缩小记录范围")
	}
	codes := make([]string, 0, len(unique))
	for code := range unique {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	jobs := make(chan []string, (len(codes)+29)/30)
	for start := 0; start < len(codes); start += 30 {
		jobs <- codes[start:min(start+30, len(codes))]
	}
	close(jobs)
	quotes := make(map[string]recordQuote)
	var mutex sync.Mutex
	var workers sync.WaitGroup
	for range min(4, len(jobs)) {
		workers.Go(func() {
			for batch := range jobs {
				if ctx.Err() != nil {
					return
				}
				found := make(map[string]recordQuote)
				for _, source := range []string{"tencent", "sina"} {
					symbols := make([]string, 0, len(batch))
					for _, code := range batch {
						if _, ok := found[code]; !ok {
							symbols = append(symbols, recordQuoteSymbol(code))
						}
					}
					if len(symbols) == 0 || ctx.Err() != nil {
						break
					}
					requestCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
					body, fetchedAt, err := fetch(requestCtx, source, symbols)
					cancel()
					if err == nil {
						for code, quote := range parseRecordQuotes(source, body, fetchedAt, symbols, time.Now()) {
							found[code] = quote
						}
					}
				}
				mutex.Lock()
				for code, quote := range found {
					quotes[code] = quote
				}
				mutex.Unlock()
			}
		})
	}
	workers.Wait()
	errors := []string{}
	if missing := len(codes) - len(quotes); missing > 0 {
		errors = append(errors, fmt.Sprintf("%d 只股票暂未取得有效报价，可稍后刷新", missing))
	}
	return map[string]any{"quotes": quotes, "errors": errors}, nil
}

// Use the provider's timestamp, not the request completion time. Malformed,
// zero, non-finite and future prices/times must not produce apparent P&L.
func parseRecordQuotes(source, body, fetchedAt string, symbols []string, now time.Time) map[string]recordQuote {
	allowed := make(map[string]bool, len(symbols))
	for _, symbol := range symbols {
		allowed[symbol] = true
	}
	quotes := make(map[string]recordQuote)
	for _, line := range strings.Split(body, ";") {
		header, value, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		prefix, separator := "v_", "~"
		if source == "sina" {
			prefix, separator = "var hq_str_", ","
		}
		symbol, ok := strings.CutPrefix(strings.TrimSpace(header), prefix)
		if !ok || !allowed[symbol] {
			continue
		}
		parts := strings.Split(strings.Trim(strings.TrimSpace(value), "\""), separator)
		var stamp time.Time
		var err error
		if source == "sina" {
			if len(parts) < 32 {
				continue
			}
			stamp, err = time.ParseInLocation("2006-01-02 15:04:05", strings.TrimSpace(parts[30])+" "+strings.TrimSpace(parts[31]), recordQuoteZone)
		} else {
			if len(parts) < 31 || strings.TrimSpace(parts[2]) != symbol[2:] {
				continue
			}
			stamp, err = time.ParseInLocation("20060102150405", strings.TrimSpace(parts[30]), recordQuoteZone)
		}
		if err != nil || stamp.After(now.Add(time.Minute)) {
			continue
		}
		price, err := strconv.ParseFloat(strings.TrimSpace(parts[3]), 64)
		if err != nil || price <= 0 || math.IsNaN(price) || math.IsInf(price, 0) {
			continue
		}
		code := symbol[2:]
		quotes[code] = recordQuote{code, price, stamp.Format(time.RFC3339), source, fetchedAt}
	}
	return quotes
}
