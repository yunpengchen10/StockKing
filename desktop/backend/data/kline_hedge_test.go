package data

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func hedgeTestRows(source string) *KLineSourceResult {
	rows := []KLineData{{Day: "2026-10-13", Close: "10"}}
	return &KLineSourceResult{Source: source, Data: &rows}
}

func awaitHedgeResult(t *testing.T, results <-chan *KLineSourceResult) *KLineSourceResult {
	t.Helper()
	select {
	case result := <-results:
		return result
	case <-time.After(time.Second):
		t.Fatal("hedged chart request did not complete")
		return nil
	}
}

func TestKLineHedgeKeepsResponsivePrimary(t *testing.T) {
	var fallbackCalls atomic.Int32
	want := hedgeTestRows("tdx-mac")
	got := fetchKLineWithHedge(func() *KLineSourceResult { return want }, func(context.Context) *KLineSourceResult {
		fallbackCalls.Add(1)
		return hedgeTestRows("sina")
	}, time.Hour)
	if got != want || fallbackCalls.Load() != 0 {
		t.Fatalf("responsive primary was displaced: result=%+v calls=%d", got, fallbackCalls.Load())
	}
}

func TestKLineHedgeDoesNotWaitForSlowPrimary(t *testing.T) {
	releasePrimary := make(chan struct{})
	defer close(releasePrimary)
	results := make(chan *KLineSourceResult, 1)
	want := hedgeTestRows("sina")
	go func() {
		results <- fetchKLineWithHedge(func() *KLineSourceResult {
			<-releasePrimary
			return hedgeTestRows("tdx-mac")
		}, func(context.Context) *KLineSourceResult { return want }, 0)
	}()
	if got := awaitHedgeResult(t, results); got != want {
		t.Fatalf("fallback did not serve pending primary: %+v", got)
	}
}

func TestKLineHedgeFailedPrimaryStartsFallbackImmediately(t *testing.T) {
	var calls atomic.Int32
	want := hedgeTestRows("tencent")
	results := make(chan *KLineSourceResult, 1)
	go func() {
		results <- fetchKLineWithHedge(func() *KLineSourceResult { return nil }, func(context.Context) *KLineSourceResult {
			calls.Add(1)
			return want
		}, time.Hour)
	}()
	if got := awaitHedgeResult(t, results); got != want || calls.Load() != 1 {
		t.Fatalf("failed primary did not promptly fall back: %+v calls=%d", got, calls.Load())
	}
}

func TestKLineHedgeEmptyFallbackCannotBeatPrimary(t *testing.T) {
	fallbackFinished := make(chan struct{})
	want := hedgeTestRows("tdx-mac")
	var fallbackCalls atomic.Int32
	got := fetchKLineWithHedge(func() *KLineSourceResult {
		<-fallbackFinished
		return want
	}, func(context.Context) *KLineSourceResult {
		fallbackCalls.Add(1)
		close(fallbackFinished)
		return &KLineSourceResult{Data: &[]KLineData{}, Source: "eastmoney"}
	}, 0)
	if got != want || fallbackCalls.Load() != 1 {
		t.Fatalf("empty fallback displaced primary: %+v calls=%d", got, fallbackCalls.Load())
	}
}

func TestKLineHedgeBothEmptyCompletesOnce(t *testing.T) {
	fallbackFinished := make(chan struct{})
	var calls atomic.Int32
	want := &KLineSourceResult{Data: &[]KLineData{}, Source: "eastmoney"}
	got := fetchKLineWithHedge(func() *KLineSourceResult {
		<-fallbackFinished
		return nil
	}, func(context.Context) *KLineSourceResult {
		calls.Add(1)
		close(fallbackFinished)
		return want
	}, 0)
	if got != want || calls.Load() != 1 {
		t.Fatalf("all-empty race duplicated fallback or lost result: %+v calls=%d", got, calls.Load())
	}
}

func TestKLineHedgeCancelsLosingHTTPFallback(t *testing.T) {
	fallbackStarted, fallbackCanceled := make(chan struct{}), make(chan struct{})
	want := hedgeTestRows("tdx-mac")
	got := fetchKLineWithHedge(func() *KLineSourceResult {
		<-fallbackStarted
		return want
	}, func(ctx context.Context) *KLineSourceResult {
		close(fallbackStarted)
		<-ctx.Done()
		close(fallbackCanceled)
		return nil
	}, 0)
	if got != want {
		t.Fatalf("primary result lost: %+v", got)
	}
	select {
	case <-fallbackCanceled:
	case <-time.After(time.Second):
		t.Fatal("losing HTTP fallback was not canceled")
	}
}

func TestKLineHedgeOnlyEquivalentLatestAdjustedDomesticBars(t *testing.T) {
	for _, period := range []string{"101", "102", "103"} {
		for _, code := range []string{"600000", "sh600000", "000001.SZ", "bj920002"} {
			for _, adjustment := range []string{"", "qfq"} {
				if !shouldHedgeLatestKLine(code, period, "", adjustment) {
					t.Errorf("eligible request did not hedge: %s/%s/%s", code, period, adjustment)
				}
			}
		}
	}
	for _, request := range [][4]string{
		{"600000", "1", "", ""}, {"600000", "5", "", "qfq"},
		{"600000", "101", "20261001", "qfq"}, {"600000", "101", "", "none"},
		{"600000", "101", "", "hfq"}, {"600000", "101", "", "0"},
		{"00700.HK", "101", "", "qfq"}, {"AAPL.US", "101", "", "qfq"},
		{"930599.CSI", "101", "", "qfq"}, {"100.DJIA", "101", "", "qfq"},
	} {
		if shouldHedgeLatestKLine(request[0], request[1], request[2], request[3]) {
			t.Errorf("non-equivalent request hedged: %v", request)
		}
	}
}

func TestKLineHTTPClientsKeepGlobalTimeoutUnchanged(t *testing.T) {
	originalTimeout := SharedHTTPClient.GetClient().Timeout
	config := &SettingConfig{Settings: &Settings{CrawlTimeOut: 2}}
	sina, tencent := NewSinaKLineApi(config), NewTencentKLineApi(config)
	chart := chartKLineHTTPClient(context.Background())
	if sina.client == SharedHTTPClient || tencent.client == SharedHTTPClient || chart == SharedHTTPClient {
		t.Fatal("chart request reuses mutable global HTTP client")
	}
	if chart.GetClient().Timeout != chartKLineHTTPTimeout || sina.client.GetClient().Timeout != 2*time.Second {
		t.Fatal("chart timeout is not bounded or configured timeout was lost")
	}
	if SharedHTTPClient.GetClient().Timeout != originalTimeout {
		t.Fatal("chart construction modified the global HTTP timeout")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := chartKLineHTTPClient(ctx).R().Get("http://127.0.0.1:1"); err == nil {
		t.Fatal("canceled chart request was not rejected")
	}
}

func TestKLineHTTPClientCancelsInFlightRequest(t *testing.T) {
	requestStarted, requestCanceled := make(chan struct{}), make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
		close(requestStarted)
		<-request.Context().Done()
		close(requestCanceled)
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	finished := make(chan error, 1)
	go func() {
		_, err := chartKLineHTTPClient(ctx).R().Get(server.URL)
		finished <- err
	}()
	select {
	case <-requestStarted:
	case <-time.After(time.Second):
		t.Fatal("test request did not reach server")
	}
	cancel()
	select {
	case err := <-finished:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("request returned %v instead of context cancellation", err)
		}
	case <-time.After(time.Second):
		t.Fatal("HTTP request did not stop after cancellation")
	}
	select {
	case <-requestCanceled:
	case <-time.After(time.Second):
		t.Fatal("server connection remained open after cancellation")
	}
}
