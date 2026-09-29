package data

// The desktop chart and the loopback research gateway use this one cache.
// Cached timestamps describe receipt, never the exchange quote time.
import (
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"
)

type marketCacheItem struct {
	result    *KLineSourceResult
	expires   time.Time
	requested int
}

var sharedBars = struct {
	sync.Mutex
	entries map[string]marketCacheItem
}{entries: make(map[string]marketCacheItem)}

func cloneKLines(r *KLineSourceResult) *KLineSourceResult {
	if r == nil {
		return &KLineSourceResult{Data: &[]KLineData{}}
	}
	c := *r
	if r.Data != nil {
		rows := append([]KLineData{}, (*r.Data)...)
		c.Data = &rows
	}
	return &c
}

func FetchKLineWithFallback(code, name, period string, count int, end string, flags ...string) *KLineSourceResult {
	flag := adjustFlagFromVariadic(flags...)
	if period == "1" || period == "5" || period == "15" || period == "30" || period == "60" {
		flag = "none"
	}
	key := fmt.Sprintf("%s|%s|%s|%s", sinaSymbolFromStockCode(code), period, end, flag)
	sharedBars.Lock()
	cached, ok := sharedBars.entries[key]
	sharedBars.Unlock()
	if ok && cached.requested >= count && time.Now().Before(cached.expires) {
		result := cloneKLines(cached.result)
		if count > 0 && result.Data != nil && len(*result.Data) > count {
			rows := (*result.Data)[len(*result.Data)-count:]
			result.Data = &rows
		}
		return result
	}
	result := fetchKLineUncached(code, name, period, count, end, flag)
	if result == nil {
		return &KLineSourceResult{Data: &[]KLineData{}}
	}
	result.FetchedAt = time.Now().Format(time.RFC3339Nano)
	result.Adjustment = flag
	if result.Adjustment == "" {
		result.Adjustment = "provider_default"
	}
	// Sina/Tencent's daily fallback is adjusted independently of the request.
	if (period == "101" || period == "102" || period == "103") && (result.Source == "sina" || result.Source == "tencent") {
		result.Adjustment = "qfq"
	}
	if len(sinaSymbolFromStockCode(code)) == 8 && !IsHKStockCode(code) && !IsUSStockCode(code) {
		normalizeSharedVolume(result, period)
	}
	if result.Data != nil && len(*result.Data) > 0 {
		sharedBars.Lock()
		if len(sharedBars.entries) > 256 {
			for k, v := range sharedBars.entries {
				if time.Now().After(v.expires) {
					delete(sharedBars.entries, k)
				}
			}
		}
		expires := time.Now().Add(10 * time.Second)
		if period == "1" {
			boundary := time.Now().Truncate(time.Minute).Add(time.Minute)
			if boundary.Before(expires) {
				expires = boundary
			}
		}
		sharedBars.entries[key] = marketCacheItem{cloneKLines(result), expires, count}
		sharedBars.Unlock()
	}
	return cloneKLines(result)
}

func normalizeSharedVolume(result *KLineSourceResult, period string) {
	result.VolumeUnit, result.AmountUnit = "shares", "CNY"
	if result.Data == nil {
		return
	}
	for i := range *result.Data {
		b := &(*result.Data)[i]
		vol, ve := strconv.ParseFloat(b.Volume, 64)
		amount, ae := strconv.ParseFloat(b.Amount, 64)
		low, _ := strconv.ParseFloat(b.Low, 64)
		high, _ := strconv.ParseFloat(b.High, 64)
		if ve != nil || math.IsNaN(vol) || math.IsInf(vol, 0) || vol < 0 {
			b.Volume = ""
			continue
		}
		multiplier := 1.0
		if result.Source == "eastmoney" || result.Source == "tencent" {
			multiplier = 100
		}
		if ae == nil && amount > 0 && vol > 0 && low > 0 && high >= low {
			implied := amount / vol
			switch {
			case implied >= low*.97 && implied <= high*1.03:
				multiplier = 1
			case implied/100 >= low*.97 && implied/100 <= high*1.03:
				multiplier = 100
			default:
				b.Volume = ""
				b.Amount = ""
				continue // Unknown units must not enter turnover/VWAP.
			}
		}
		b.Volume = strconv.FormatFloat(vol*multiplier, 'f', -1, 64)
		if ae != nil || amount < 0 || math.IsNaN(amount) || math.IsInf(amount, 0) || (amount == 0 && vol > 0) {
			b.Amount = ""
		}
	}
}

type rawQuoteCacheItem struct {
	body string
	at   time.Time
}

var sharedRawQuotes = struct {
	sync.Mutex
	entries map[string]rawQuoteCacheItem
}{entries: make(map[string]rawQuoteCacheItem)}

func FetchSharedQuotePayload(source string, symbols []string) (string, string, error) {
	codes := strings.Join(symbols, ",")
	key := source + ":" + codes
	sharedRawQuotes.Lock()
	item, ok := sharedRawQuotes.entries[key]
	sharedRawQuotes.Unlock()
	if ok && time.Since(item.at) < 2*time.Second {
		return item.body, item.at.Format(time.RFC3339Nano), nil
	}
	address, referer := "https://qt.gtimg.cn/q="+codes, "https://gu.qq.com/"
	if source == "sina" {
		address, referer = "https://hq.sinajs.cn/list="+codes, "https://finance.sina.com.cn/"
	}
	response, err := SharedHTTPClient.R().SetHeader("Referer", referer).SetHeader("User-Agent", "Mozilla/5.0").Get(address)
	if err != nil {
		return "", "", err
	}
	if response.StatusCode() != 200 {
		return "", "", fmt.Errorf("quote status %d", response.StatusCode())
	}
	item = rawQuoteCacheItem{GB18030ToUTF8(response.Body()), time.Now()}
	sharedRawQuotes.Lock()
	if len(sharedRawQuotes.entries) > 512 {
		sharedRawQuotes.entries = make(map[string]rawQuoteCacheItem)
	}
	sharedRawQuotes.entries[key] = item
	sharedRawQuotes.Unlock()
	return item.body, item.at.Format(time.RFC3339Nano), nil
}
