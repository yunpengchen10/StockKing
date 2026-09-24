package main

import (
	"go-stock/backend/data"
	"go-stock/backend/db"
	"os"
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
}
