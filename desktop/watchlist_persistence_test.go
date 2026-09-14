package main

import (
	"path/filepath"
	"testing"

	"go-stock/backend/data"
	"go-stock/backend/db"
)

func TestWatchlistPersistsWithoutRealtimeQuote(t *testing.T) {
	db.Init(filepath.Join(t.TempDir(), "watchlist.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatalf("open test database handle: %v", err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err := db.Dao.AutoMigrate(&data.Settings{}, &data.AIConfig{}, &data.StockBasic{}, &data.FollowedStock{}); err != nil {
		t.Fatalf("migrate watchlist test database: %v", err)
	}
	if err := db.Dao.Create(&data.StockBasic{TsCode: "300750.SZ", Symbol: "300750", Name: "宁德时代"}).Error; err != nil {
		t.Fatalf("seed stock basic: %v", err)
	}

	api := data.NewStockDataApi()
	if got := api.Follow("300750.SZ"); got != "关注成功" {
		t.Fatalf("follow result = %q", got)
	}
	list := api.GetFollowList(0)
	if list == nil || len(*list) != 1 {
		t.Fatalf("persisted watchlist size = %d", len(*list))
	}
	if (*list)[0].StockCode != "sz300750" || (*list)[0].Name != "宁德时代" {
		t.Fatalf("unexpected persisted stock: %+v", (*list)[0])
	}

	api.UnFollow("sz300750")
	if got := api.GetFollowList(0); got == nil || len(*got) != 0 {
		t.Fatalf("watchlist should be empty after unfollow")
	}
	if got := api.Follow("300750.SZ"); got != "关注成功" {
		t.Fatalf("re-follow result = %q", got)
	}
	if got := api.GetFollowList(0); got == nil || len(*got) != 1 {
		t.Fatalf("soft-deleted stock was not restored")
	}
}
