package main

import (
	"github.com/glebarez/sqlite"
	"go-stock/backend/data"
	"go/ast"
	"go/parser"
	"go/token"
	"gorm.io/gorm"
	"path/filepath"
	"testing"
)

func TestScheduledCallbacksCannotReachLegacyAIDispatch(t *testing.T) {
	source, err := parser.ParseFile(token.NewFileSet(), "app.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	callbacks := 0
	ast.Inspect(source, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || selector.Sel.Name != "AddFunc" {
			return true
		}
		callbacks++
		ast.Inspect(call, func(child ast.Node) bool {
			if sel, ok := child.(*ast.SelectorExpr); ok && (sel.Sel.Name == "ExecuteTask" || sel.Sel.Name == "NewChatStream" || sel.Sel.Name == "NewDeepSeekOpenAi") {
				t.Errorf("scheduled callback reaches billable %s", sel.Sel.Name)
			}
			return true
		})
		return true
	})
	if callbacks < 4 {
		t.Fatal("schedule coverage missing")
	}
	for _, declaration := range source.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if !ok || function.Name.Name != "AddCronTask" {
			continue
		}
		ast.Inspect(function, func(node ast.Node) bool {
			if sel, ok := node.(*ast.SelectorExpr); ok && (sel.Sel.Name == "NewChatStream" || sel.Sel.Name == "NewDeepSeekOpenAi") {
				t.Errorf("watchlist schedule reaches %s", sel.Sel.Name)
			}
			return true
		})
	}
}

func TestSelectionBaselineMigrationAndLateReply(t *testing.T) {
	dao, err := gorm.Open(sqlite.Open(filepath.Join(t.TempDir(), "watch.db")), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	sqlDB, _ := dao.DB()
	t.Cleanup(func() { sqlDB.Close() })
	if err = dao.AutoMigrate(&data.FollowedStock{}); err != nil {
		t.Fatal(err)
	}
	old := data.FollowedStock{StockCode: "sh600001", Price: 10}
	dao.Create(&old)
	q := data.StockInfo{Code: "sh600001", Price: "11", Date: "2026-09-10", Time: "10:30:00"}
	data.ApplyLatestWatchQuote(dao, old.StockCode, q)
	var got data.FollowedStock
	dao.First(&got, "stock_code = ?", old.StockCode)
	if got.SelectionPrice != nil {
		t.Fatal("legacy baseline backfilled")
	}
	dao.Model(&data.FollowedStock{}).Where("stock_code = ?", old.StockCode).Updates(map[string]any{"selection_id": "new", "selection_status": "pending"})
	data.ApplySelectionQuote(dao, old.StockCode, "old", q)
	dao.First(&got, "stock_code = ?", old.StockCode)
	if got.SelectionPrice != nil {
		t.Fatal("late generation accepted")
	}
	data.ApplySelectionQuote(dao, old.StockCode, "new", q)
	q.Price = "12"
	data.ApplySelectionQuote(dao, old.StockCode, "new", q)
	data.ApplyLatestWatchQuote(dao, old.StockCode, q)
	dao.First(&got, "stock_code = ?", old.StockCode)
	if got.SelectionPrice == nil || *got.SelectionPrice != 11 || got.Price != 12 {
		t.Fatal("baseline changed on refresh")
	}
}

func TestLegacyWatchlistSchemaUpgradeAndRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "legacy.db")
	dao, err := gorm.Open(sqlite.Open(path), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	connection, _ := dao.DB()
	if err := dao.Exec("CREATE TABLE followed_stock (id integer PRIMARY KEY, stock_code text, price real, time datetime, is_del integer)").Error; err != nil {
		t.Fatal(err)
	}
	dao.Exec("INSERT INTO followed_stock VALUES (1, 'sh600001', 10, '2026-08-01 09:30:00+08:00', 0)")
	if err := dao.AutoMigrate(&data.FollowedStock{}); err != nil {
		t.Fatal(err)
	}
	connection.Close()
	restarted, err := gorm.Open(sqlite.Open(path), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	connection, _ = restarted.DB()
	t.Cleanup(func() { connection.Close() })
	if err := restarted.AutoMigrate(&data.FollowedStock{}); err != nil {
		t.Fatal(err)
	}
	var row data.FollowedStock
	if err := restarted.First(&row, "stock_code = ?", "sh600001").Error; err != nil {
		t.Fatal(err)
	}
	if row.SelectionPrice != nil || row.SelectionID != "" || row.SelectionStatus != "" || row.Price != 10 || row.Time.Year() != 2026 || row.Time.Month() != 8 {
		t.Fatalf("legacy row changed after upgrade/restart: %#v", row)
	}
}

func TestWatchGroupsKeepStocksAndRollbackDeletion(t *testing.T) {
	dao, err := gorm.Open(sqlite.Open(filepath.Join(t.TempDir(), "groups.db")), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	sqlDB, _ := dao.DB()
	t.Cleanup(func() { sqlDB.Close() })
	dao.AutoMigrate(&data.Group{}, &data.GroupStock{}, &data.FollowedStock{})
	api := data.NewStockGroupApi(dao)
	if err := api.SaveGroup(0, " Growth "); err != nil {
		t.Fatal(err)
	}
	if api.SaveGroup(0, "growth") == nil || api.SaveGroup(0, "全部") == nil {
		t.Fatal("invalid name accepted")
	}
	group := api.GetGroupList()[0]
	entry := 10.0
	dao.Create(&data.FollowedStock{StockCode: "sh600001", SelectionPrice: &entry, SelectionID: "immutable"})
	if !api.AddStockGroup(int(group.ID), "sh600001") {
		t.Fatal("membership failed")
	}
	if err := api.SaveGroup(int(group.ID), "长期"); err != nil {
		t.Fatal(err)
	}
	if err := api.SaveGroup(0, "第二组"); err != nil {
		t.Fatal(err)
	}
	second := api.GetGroupList()[1]
	if !api.AddStockGroup(int(second.ID), "sh600001") || !api.AddStockGroup(int(second.ID), "sh600001") {
		t.Fatal("multi-group addition failed")
	}
	if len(api.GetAllGroupStocks()) != 2 {
		t.Fatal("repeated membership was duplicated")
	}
	dao.Exec("CREATE TRIGGER block_group_delete BEFORE UPDATE OF deleted_at ON stock_groups BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
	if api.DeleteGroup(int(group.ID)) == nil {
		t.Fatal("expected transaction failure")
	}
	if len(api.GetAllGroupStocks()) != 2 {
		t.Fatal("membership deletion did not roll back")
	}
	dao.Exec("DROP TRIGGER block_group_delete")
	if err := api.DeleteGroup(int(group.ID)); err != nil {
		t.Fatal(err)
	}
	var count int64
	dao.Model(&data.FollowedStock{}).Count(&count)
	if count != 1 || len(api.GetAllGroupStocks()) != 1 || api.GetAllGroupStocks()[0].GroupId != int(second.ID) {
		t.Fatal("group deletion removed watchlist")
	}
	var watched data.FollowedStock
	dao.First(&watched, "stock_code = ?", "sh600001")
	if watched.SelectionPrice == nil || *watched.SelectionPrice != 10 || watched.SelectionID != "immutable" {
		t.Fatal("group changes reset baseline")
	}
}
