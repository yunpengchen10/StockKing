package data

import (
	"context"
	"strings"
	"time"

	"github.com/go-resty/resty/v2"
)

const (
	chartKLineHedgeDelay  = 300 * time.Millisecond
	chartKLineHTTPTimeout = 4 * time.Second
)

func hasKLineRows(result *KLineSourceResult) bool {
	return result != nil && result.Data != nil && len(*result.Data) > 0
}

// Only equivalent latest adjusted domestic bars may race. Historical cursors,
// minute bars and explicit non/after-adjusted requests keep their source order.
func shouldHedgeLatestKLine(code, period, end, adjustment string) bool {
	if strings.TrimSpace(end) != "" || (period != "101" && period != "102" && period != "103") {
		return false
	}
	if adjustment != "" && adjustment != "qfq" {
		return false
	}
	if IsHKStockCode(code) || IsUSStockCode(code) || IsCSIIndexCode(code) || IsGlobalIndexCode(code) {
		return false
	}
	symbol := sinaSymbolFromStockCode(code)
	return len(symbol) == 8 && (strings.HasPrefix(symbol, "sh") || strings.HasPrefix(symbol, "sz") || strings.HasPrefix(symbol, "bj"))
}

// Keep the preferred provider when it is responsive; otherwise let a bounded
// HTTP fallback serve the chart while MAC finishes its initial node discovery.
// Buffered channels let a losing provider finish without blocking or overwriting
// the chosen result. Empty responses never beat a pending successful provider.
func fetchKLineWithHedge(primary func() *KLineSourceResult, fallback func(context.Context) *KLineSourceResult, delay time.Duration) *KLineSourceResult {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	primaryResults := make(chan *KLineSourceResult, 1)
	go func() { primaryResults <- primary() }()
	timer := time.NewTimer(delay)
	defer timer.Stop()
	var fallbackResults chan *KLineSourceResult
	fallbackStarted := false
	startFallback := func() {
		if fallbackStarted {
			return
		}
		fallbackStarted = true
		fallbackResults = make(chan *KLineSourceResult, 1)
		results := fallbackResults
		go func() { results <- fallback(ctx) }()
	}
	var fallbackResult *KLineSourceResult
	primaryDone, fallbackDone := false, false
	for !primaryDone || !fallbackDone {
		select {
		case result := <-primaryResults:
			if hasKLineRows(result) {
				return result
			}
			primaryDone = true
			primaryResults = nil
			startFallback()
		case result := <-fallbackResults:
			if hasKLineRows(result) {
				return result
			}
			fallbackResult = result
			fallbackDone = true
			// A nil channel disables this select case after its sole response.
			fallbackResults = nil
		case <-timer.C:
			if !fallbackDone {
				startFallback()
			}
		}
	}
	return fallbackResult
}

func chartKLineConfig() *SettingConfig {
	config := *GetSettingConfig()
	settings := *config.Settings
	settings.CrawlTimeOut = int64(chartKLineHTTPTimeout / time.Second)
	config.Settings = &settings
	return &config
}

func chartKLineHTTPClient(ctx context.Context) *resty.Client {
	return CreateHTTPClientWithTimeout(chartKLineHTTPTimeout).OnBeforeRequest(func(_ *resty.Client, request *resty.Request) error {
		request.SetContext(ctx)
		return ctx.Err()
	})
}

func fetchLatestHTTPKLine(ctx context.Context, code, name, period string, count int, adjustment string) *KLineSourceResult {
	result := fetchFromEastMoneyContext(ctx, code, name, period, count, "", adjustment)
	result.Source = "eastmoney"
	if hasKLineRows(result) || ctx.Err() != nil {
		return result
	}
	sina := fetchFromSinaContext(ctx, code, period, count)
	sina.Source = "sina"
	if hasKLineRows(sina) || ctx.Err() != nil {
		return sina
	}
	tencent := fetchFromTencentContext(ctx, code, period, count)
	tencent.Source = "tencent"
	if hasKLineRows(tencent) {
		return tencent
	}
	return result
}
