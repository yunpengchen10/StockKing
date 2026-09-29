package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"go-stock/backend/data"
	"go-stock/backend/models"
	"math"
	"net"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var mainMarketCode = regexp.MustCompile(`^[036][0-9]{5}$`)
var shanghaiMarketTZ = time.FixedZone("Asia/Shanghai", 8*3600)

func startMarketGateway(ctx context.Context) (string, string, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", "", err
	}
	token, err := randomToken()
	if err != nil {
		_ = listener.Close()
		return "", "", err
	}
	server := &http.Server{Handler: marketGatewayHandler(token), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 120 * time.Second, IdleTimeout: 20 * time.Second}
	go func() { <-ctx.Done(); _ = server.Close() }()
	go func() { _ = server.Serve(listener) }()
	return "http://" + listener.Addr().String(), token, nil
}

func marketGatewayHandler(token string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host, _, _ := net.SplitHostPort(r.RemoteAddr)
		if host != "127.0.0.1" || subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Stock-King-Market-Token")), []byte(token)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, "POST required", 405)
			return
		}
		// This interface has no arbitrary URLs, file paths, SQL or execution methods.
		var request struct {
			Codes  []string `json:"codes"`
			Period string   `json:"period"`
			Count  int      `json:"count"`
			End    string   `json:"end"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384)).Decode(&request); err != nil {
			http.Error(w, "invalid request", 400)
			return
		}
		if len(request.Codes) > 30 {
			http.Error(w, "maximum 30 symbols", 400)
			return
		}
		for _, code := range request.Codes {
			if !mainMarketCode.MatchString(code) {
				http.Error(w, "invalid symbol", 400)
				return
			}
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		var payload any
		switch r.URL.Path {
		case "/v1/quotes":
			symbols := make([]string, 0, len(request.Codes))
			for _, code := range request.Codes {
				prefix := "sz"
				if code[0] == '6' {
					prefix = "sh"
				}
				symbols = append(symbols, prefix+code)
			}
			sources := map[string]any{}
			if len(symbols) > 0 {
				for _, source := range []string{"tencent", "sina"} {
					body, at, err := data.FetchSharedQuotePayload(source, symbols)
					if err == nil {
						sources[source] = map[string]any{"payload": body, "fetched_at": at}
					}
				}
			}
			payload = map[string]any{"sources": sources}
		case "/v1/bars":
			if request.Period == "" {
				request.Period = "1"
			}
			if request.Period != "1" && request.Period != "5" && request.Period != "15" && request.Period != "101" {
				http.Error(w, "invalid period", 400)
				return
			}
			if request.Count <= 0 {
				request.Count = 5000
			}
			if request.Count > 8000 {
				request.Count = 8000
			}
			if request.End != "" && !regexp.MustCompile(`^\d{8}(\d{6})?$`).MatchString(request.End) {
				http.Error(w, "invalid end", 400)
				return
			}
			results := map[string]any{}
			for _, code := range request.Codes {
				raw := data.FetchKLineWithFallback(code, "", request.Period, request.Count, request.End, "none")
				results[code] = normalizedGatewayBars(raw, request.Period, time.Now())
			}
			payload = map[string]any{"results": results}
		case "/v1/snapshot":
			rows := []map[string]any{}
			complete := false
			seen := map[string]bool{}
			expected, pages, received := 0, 0, 0
			for page := 1; page <= 40; page++ {
				result := data.NewStockDataApi().GetAllStocks(page, 500, "", models.TechnicalIndicators{}, true)
				if result == nil || !result.Success || len(result.Result.Data) == 0 {
					break
				}
				expected = result.Result.Count
				pages = page
				received += len(result.Result.Data)
				for _, s := range result.Result.Data {
					if seen[s.SECURITYCODE] {
						continue
					}
					seen[s.SECURITYCODE] = true
					if !mainMarketCode.MatchString(s.SECURITYCODE) {
						continue
					}
					rows = append(rows, map[string]any{"code": s.SECURITYCODE, "name": s.SECURITYNAMEABBR, "price": s.NEWPRICE, "change_pct": s.CHANGERATE, "amount": finiteMarketNumber(fmt.Sprint(s.DEALAMOUNT)), "volume": snapshotVolumeShares(s), "volume_unit": "shares", "amount_unit": "CNY", "volume_ratio": s.VOLUMERATIO, "turnover_rate": s.TURNOVERRATE, "high": s.HIGHPRICE, "low": s.LOWPRICE, "previous_close": s.PRECLOSEPRICE, "industry": s.INDUSTRY, "source_time": s.MAXTRADEDATE})
				}
				if !result.Result.Nextpage {
					complete = len(seen) == result.Result.Count
					break
				}
			}
			payload = map[string]any{"items": rows, "complete": complete, "expected_count": expected, "unique_count": len(seen), "received_count": received, "pages": pages, "source": "eastmoney", "fetched_at": time.Now().Format(time.RFC3339Nano), "source_time_meaning": "provider field only; snapshot is for universe selection"}
		case "/v1/sectors":
			payload = map[string]any{"data": data.NewMarketNewsApi().GetIndustryRank("desc", 100), "source": "tencent", "fetched_at": time.Now().Format(time.RFC3339Nano)}
		default:
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(payload)
	})
}

func finiteMarketNumber(value string) any {
	number, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
	if err != nil || math.IsNaN(number) || math.IsInf(number, 0) {
		return nil
	}
	return number
}

func snapshotVolumeShares(s models.StockInfo) any {
	volume, vok := finiteMarketNumber(fmt.Sprint(s.VOLUME)).(float64)
	amount, aok := finiteMarketNumber(fmt.Sprint(s.DEALAMOUNT)).(float64)
	low, lok := finiteMarketNumber(fmt.Sprint(s.LOWPRICE)).(float64)
	high, hok := finiteMarketNumber(fmt.Sprint(s.HIGHPRICE)).(float64)
	if !vok || !aok || !lok || !hok || volume <= 0 || amount <= 0 || low <= 0 || high < low {
		return nil
	}
	implied := amount / volume
	if implied >= low*.97 && implied <= high*1.03 {
		return volume
	}
	if implied/100 >= low*.97 && implied/100 <= high*1.03 {
		return volume * 100
	}
	return nil
}

func normalizedGatewayBars(raw *data.KLineSourceResult, period string, now time.Time) map[string]any {
	rows := []map[string]any{}
	if raw == nil {
		return map[string]any{"bars": rows}
	}
	// A cached unfinished bar does not become final merely because time passed.
	if fetched, err := time.Parse(time.RFC3339Nano, raw.FetchedAt); err == nil && fetched.Before(now) {
		now = fetched
	}
	if raw.Data != nil {
		for _, b := range *raw.Data {
			var end time.Time
			for _, layout := range []string{"2006-01-02 15:04:05", "2006-01-02 15:04", "2006-01-02"} {
				parsed, err := time.ParseInLocation(layout, b.Day, shanghaiMarketTZ)
				if err == nil {
					end = parsed
					break
				}
			}
			if end.IsZero() {
				continue
			}
			if period == "101" {
				end = time.Date(end.Year(), end.Month(), end.Day(), 15, 0, 0, 0, shanghaiMarketTZ)
			}
			if end.After(now) {
				continue
			}
			if period != "101" {
				clock := end.Format("15:04")
				if !(clock >= "09:31" && clock <= "11:30" || clock >= "13:01" && clock <= "15:00") {
					continue
				}
			}
			rows = append(rows, map[string]any{"end": end.Format(time.RFC3339), "open": finiteMarketNumber(b.Open), "high": finiteMarketNumber(b.High), "low": finiteMarketNumber(b.Low), "close": finiteMarketNumber(b.Close), "volume_shares": finiteMarketNumber(b.Volume), "amount_cny": finiteMarketNumber(b.Amount), "source": raw.Source, "fetched_at": raw.FetchedAt})
		}
	}
	return map[string]any{"bars": rows, "source": raw.Source, "fetched_at": raw.FetchedAt, "volume_unit": raw.VolumeUnit, "amount_unit": raw.AmountUnit, "adjustment": raw.Adjustment, "time_semantics": "bar_end", "count": len(rows), "contract_version": "stock-king-market-v1"}
}
