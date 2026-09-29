package main

import (
	"bytes"
	"context"
	"encoding/json"
	"go-stock/backend/data"
	"go-stock/backend/db"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// Optional live check of the same Wails methods used when opening a chart.
// Ordinary test runs stay offline.
func TestStockKingLiveChartSmoke(t *testing.T) {
	if os.Getenv("STOCK_KING_LIVE_CHART_PROBE") != "1" {
		t.Skip("set STOCK_KING_LIVE_CHART_PROBE=1 to check public chart sources")
	}
	db.Init(filepath.Join(t.TempDir(), "chart.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err := db.Dao.AutoMigrate(&data.Settings{}, &data.AIConfig{}); err != nil {
		t.Fatal(err)
	}
	app := &App{}
	for _, interval := range []string{"101", "1", "5"} {
		t.Run(interval, func(t *testing.T) {
			result := app.GetStockKLineWithFallback("sh600000", "浦发银行", interval, 30, "qfq")
			if result == nil || result.Data == nil || len(*result.Data) == 0 || result.Source == "" {
				t.Fatalf("chart interval %s returned no sourced data", interval)
			}
			t.Logf("interval=%s source=%s bars=%d", interval, result.Source, len(*result.Data))
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	endpoint, token, err := startMarketGateway(ctx)
	if err != nil {
		t.Fatal(err)
	}
	request, _ := http.NewRequest("POST", endpoint+"/v1/bars", bytes.NewBufferString(`{"codes":["600000"],"period":"1","count":30}`))
	request.Header.Set("X-Stock-King-Market-Token", token)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var packet struct {
		Results map[string]struct {
			Source string           `json:"source"`
			Bars   []map[string]any `json:"bars"`
		} `json:"results"`
	}
	if err = json.NewDecoder(response.Body).Decode(&packet); err != nil {
		t.Fatal(err)
	}
	got := packet.Results["600000"]
	chart := app.GetStockKLineWithFallback("sh600000", "浦发银行", "1", 30, "none")
	if chart == nil || chart.Data == nil || len(got.Bars) == 0 || got.Source != chart.Source {
		t.Fatal("chart/gateway source mismatch or empty minute data")
	}
	last := (*chart.Data)[len(*chart.Data)-1]
	bar := got.Bars[len(got.Bars)-1]
	for key, value := range map[string]string{"open": last.Open, "high": last.High, "low": last.Low, "close": last.Close, "volume_shares": last.Volume, "amount_cny": last.Amount} {
		if bar[key] != finiteMarketNumber(value) {
			t.Fatalf("chart/gateway %s mismatch %v/%s", key, bar[key], value)
		}
	}
	t.Logf("authenticated gateway/chart identical: source=%s completed_bars=%d", got.Source, len(got.Bars))
	pythonPath := filepath.Join("..", ".venv", "Scripts", "python.exe")
	if _, err := os.Stat(pythonPath); err == nil {
		absolutePython, _ := filepath.Abs(pythonPath)
		probe := exec.Command(absolutePython, "-c", `import json
from src.services.software_market import SoftwareMarketClient
from src.services.yao_scout.minute_history import normalize_bars
from src.services.yao_scout.daily_opportunities import mainboard
from datetime import datetime
from zoneinfo import ZoneInfo
c=SoftwareMarketClient.from_environment()
p=c.bars('600000',count=8000)
bars=normalize_bars(p['bars'],datetime.now(ZoneInfo('Asia/Shanghai')))
assert len(bars)>200, ('no valid software minute history',p.get('source'),len(p['bars']))
q=c.quotes(['600000'])
assert q['quotes'][0]['quote']['price']>0
frame=c.snapshot()
assert frame.attrs['complete'], ('snapshot incomplete',len(frame),frame.attrs)
assert len(mainboard(frame))>2500
print(json.dumps({'bridge':'ok','source':p['source'],'validMinuteBars':len(bars),'snapshotRows':len(frame),'mainboardRows':len(mainboard(frame))}))`)
		probe.Dir = filepath.Join("..", "daily-engine")
		probe.Env = append(os.Environ(), "STOCK_KING_MARKET_URL="+endpoint, "STOCK_KING_MARKET_TOKEN="+token)
		output, err := probe.CombinedOutput()
		if err != nil {
			t.Fatalf("Python bridge probe: %v %s", err, output)
		}
		t.Log(string(output))
	}
}
