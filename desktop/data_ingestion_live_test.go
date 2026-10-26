package main

import (
	"context"
	"go-stock/backend/data"
	"go-stock/backend/db"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// Opt-in public-data check through the same authenticated gateway as the app.
// Credentials stay in the child environment; the probe must print no secrets.
func TestStockKingDataIngestionLiveProbe(t *testing.T) {
	probe := os.Getenv("STOCK_KING_DATA_PROBE_SCRIPT")
	python := os.Getenv("STOCK_KING_DATA_PROBE_PYTHON")
	if probe == "" || python == "" {
		t.Skip("opt-in public market ingestion probe")
	}
	db.Init(filepath.Join(t.TempDir(), "probe.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err = db.Dao.AutoMigrate(&data.Settings{}, &data.AIConfig{}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	endpoint, token, err := startMarketGateway(ctx)
	if err != nil {
		t.Fatal(err)
	}
	command := exec.CommandContext(ctx, python, probe)
	command.Dir = filepath.Join("..", "daily-engine")
	command.Env = append(os.Environ(), "STOCK_KING_MARKET_URL="+endpoint, "STOCK_KING_MARKET_TOKEN="+token)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("data probe: %v\n%s", err, output)
	}
	t.Log(string(output))
}
