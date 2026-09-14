package main

import (
	"fmt"
	"go-stock/backend/data"
	"go-stock/backend/db"
	"go-stock/backend/models"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestStockKingPathsAreIsolated(t *testing.T) {
	roaming := filepath.Join(t.TempDir(), "roaming")
	local := filepath.Join(t.TempDir(), "local")
	t.Setenv("APPDATA", roaming)
	t.Setenv("LOCALAPPDATA", local)

	paths, err := resolveAppPaths()
	if err != nil {
		t.Fatal(err)
	}
	if paths.RoamingRoot != filepath.Join(roaming, "Stock King") {
		t.Fatalf("unexpected roaming root: %s", paths.RoamingRoot)
	}
	if paths.LocalRoot != filepath.Join(local, "Stock King") {
		t.Fatalf("unexpected local root: %s", paths.LocalRoot)
	}
	dsn := strings.ToLower(paths.databaseDSN())
	if strings.Contains(dsn, "go-stock") || strings.Contains(dsn, "daily_stock_analysis") {
		t.Fatalf("database DSN reused a legacy project path: %s", dsn)
	}
}

func TestDailyRuleObservationsDoNotBecomeLegacyReturnSamples(t *testing.T) {
	// No database is needed: these observations must exit before any legacy
	// reference-price row is queried or written.
	result := map[string]any{"adaptive": map[string]any{"scanSlot": "live", "candidates": []any{
		map[string]any{"code": "600001", "name": "观察样本", "model_version": "king-daily-rules-20260908", "referencePrice": 12.0},
	}}}
	count, err := syncKingPicksToRecommendations(result)
	if err != nil || count != 0 {
		t.Fatalf("observation became legacy sample: count=%d err=%v", count, err)
	}
}

func TestStockKingAIAdviceIsSavedAndListed(t *testing.T) {
	db.Init(filepath.Join(t.TempDir(), "ai-advice.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err := db.Dao.AutoMigrate(&models.AIResponseResult{}); err != nil {
		t.Fatal(err)
	}

	app := &App{}
	saved, err := app.SaveStockKingAIAdvice("sh600000", "浦发银行", "# 结构化建议\n\n保留证据链。", "测试模型")
	if err != nil {
		t.Fatal(err)
	}
	if saved.StockCode != "600000.SH" || saved.StockName != "浦发银行" {
		t.Fatalf("unexpected saved advice: %#v", saved)
	}
	history, err := app.GetStockKingAIAdviceHistory("600000.SH", 12)
	if err != nil {
		t.Fatal(err)
	}
	if len(history) != 1 || history[0].Content != saved.Content || history[0].Question != "Stock King 结构化研究建议" {
		t.Fatalf("unexpected advice history: %#v", history)
	}
}

func TestHistoricalKLineRequestSkipsLatestOnlyMACSource(t *testing.T) {
	if !data.ShouldUseLatestMACKLine("") || !data.ShouldUseLatestMACKLine("   ") {
		t.Fatal("latest K line requests should retain the MAC source")
	}
	if data.ShouldUseLatestMACKLine("20260822145959") {
		t.Fatal("historical K line requests must use a source that honors the end cursor")
	}
}

func TestKingPicksSyncToRecommendationHistoryWithoutDuplicates(t *testing.T) {
	db.Init(filepath.Join(t.TempDir(), "king-picks.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err := db.Dao.AutoMigrate(&models.AiRecommendStocks{}); err != nil {
		t.Fatal(err)
	}

	result := map[string]any{
		"generatedAt": "2026-08-21T09:30:00+08:00",
		"nonKeChuang": []any{map[string]any{
			"symbol":          map[string]any{"code": "sh600000", "name": "浦发银行"},
			"rank":            float64(1),
			"score":           87.5,
			"screeningSource": "全市场质量筛选",
			"forecastSummary": "5日方向偏多",
			"riskDecision":    "通过风险覆盖",
			"degradedReasons": []any{"新闻覆盖不足"},
			"boardGroup":      "主板",
			"industry":        "银行",
			"price":           "10.25",
		}},
		"kechuang": []any{},
	}
	for run := 0; run < 2; run++ {
		count, err := syncKingPicksToRecommendations(result)
		if err != nil || count != 1 {
			t.Fatalf("sync run %d failed: count=%d err=%v", run, count, err)
		}
	}
	var records []models.AiRecommendStocks
	if err := db.Dao.Find(&records).Error; err != nil {
		t.Fatal(err)
	}
	if len(records) != 1 {
		t.Fatalf("expected one deduplicated recommendation, got %#v", records)
	}
	record := records[0]
	if record.ModelName != "King 每日精选" || record.StockCode != "600000.SH" || record.StockPrice != "10.25" {
		t.Fatalf("unexpected recommendation: %#v", record)
	}
	if !strings.Contains(record.RecommendReason, "5日方向偏多") || !strings.Contains(record.RiskRemarks, "新闻覆盖不足") {
		t.Fatalf("recommendation evidence was not preserved: %#v", record)
	}
}

func TestKingPicksSyncsSixTierBoardGroups(t *testing.T) {
	db.Init(filepath.Join(t.TempDir(), "king-tier-picks.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err := db.Dao.AutoMigrate(&models.AiRecommendStocks{}); err != nil {
		t.Fatal(err)
	}

	code := 600100
	tiers := map[string]any{}
	for _, tier := range []string{"conservative", "regular", "aggressive"} {
		payload := map[string]any{}
		for _, board := range []string{"nonKeChuang", "kechuang"} {
			code++
			pick := map[string]any{
				"symbol":             map[string]any{"code": fmt.Sprintf("%06d.SH", code), "name": fmt.Sprintf("测试%d", code)},
				"rank":               float64(1),
				"tierScore":          88.0,
				"recommendationTier": tier,
				"tierReasons":        []any{"档位证据"},
				"screeningSource":    "King 三档策略",
				"boardGroup":         board,
				"industry":           "测试行业",
				"price":              "12.34",
			}
			if tier == "aggressive" {
				pick["potentialScore"] = 92.5
				pick["potentialReasons"] = []any{"动量与活跃度领先"}
				pick["potentialDegradedReasons"] = []any{"量化结果不可用但不扣分"}
			}
			payload[board] = []any{pick}
		}
		tiers[tier] = payload
	}
	result := map[string]any{
		"generatedAt": "2026-08-25T09:30:00+08:00",
		"tiers":       tiers,
	}
	count, err := syncKingPicksToRecommendations(result)
	if err != nil || count != 6 {
		t.Fatalf("six-group sync failed: count=%d err=%v", count, err)
	}
	var records []models.AiRecommendStocks
	if err := db.Dao.Order("stock_code").Find(&records).Error; err != nil {
		t.Fatal(err)
	}
	if len(records) != 6 {
		t.Fatalf("expected six unique recommendation records, got %#v", records)
	}
	aggressive := records[len(records)-1]
	if !strings.Contains(aggressive.Rating, "激进百分位") || !strings.Contains(aggressive.RecommendReason, "潜力证据") || !strings.Contains(aggressive.Remarks, "激进推荐") {
		t.Fatalf("aggressive evidence or labels were not preserved: %#v", aggressive)
	}
}

func TestWatchlistNotesAndLayoutsSurviveDatabaseReopen(t *testing.T) {
	databasePath := filepath.Join(t.TempDir(), "stock-king.db")
	open := func() {
		db.Init(filepath.ToSlash(databasePath) + "?_busy_timeout=10000&_journal_mode=WAL")
		if err := db.Dao.AutoMigrate(
			&data.FollowedStock{}, &data.Group{}, &data.GroupStock{}, &data.Settings{}, &data.AIConfig{},
			&ResearchNote{}, &MultiKlineLayout{}, &StockKingPreference{},
		); err != nil {
			t.Fatal(err)
		}
	}
	open()
	t.Cleanup(func() {
		if current, err := db.Dao.DB(); err == nil {
			_ = current.Close()
		}
	})
	app := &App{}

	if result := app.Follow("sh600519"); result != "关注成功" {
		t.Fatalf("follow failed: %s", result)
	}
	if result := app.Follow("sz300750"); result != "关注成功" {
		t.Fatalf("second follow failed: %s", result)
	}
	if result := app.AddGroup(data.Group{Name: "核心观察", Sort: 1}); result != "添加成功" {
		t.Fatalf("add group failed: %s", result)
	}
	groups := app.GetGroupList()
	if len(groups) != 1 {
		t.Fatalf("expected one group, got %d", len(groups))
	}
	if result := app.AddStockGroup(int(groups[0].ID), "sh600519"); result != "添加成功" {
		t.Fatalf("assign group failed: %s", result)
	}
	app.SetStockSort(0, "sz300750")

	_, err := app.SaveResearchNote(ResearchNote{
		SymbolCode: "600519.SH", SymbolName: "贵州茅台",
		CoreLogic: "品牌与现金流", ObservationPlan: "观察量价", InvalidationRisk: "渠道库存恶化",
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = app.SaveMultiKlineLayout(MultiKlineLayout{
		Name: "核心组合", GridSize: 4, SyncPeriod: true, SyncCrosshair: true,
		Period: "101", SymbolsJSON: `["600519.SH","300750.SZ"]`, IndicatorsJSON: `["MA","MACD"]`,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := app.SaveStockKingPreference("kline.lastSymbol", "600519.SH"); err != nil {
		t.Fatal(err)
	}
	adviceSession := `{"schemaVersion":1,"code":"600519.SH","name":"贵州茅台","advice":{"summary":"保留上次建议"}}`
	if err := app.SaveStockKingPreference("aiAdvice.lastSession.v1", adviceSession); err != nil {
		t.Fatal(err)
	}

	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	if err := sqlDB.Close(); err != nil {
		t.Fatal(err)
	}
	open()

	if follows := app.GetFollowList(0); follows == nil || len(*follows) != 2 {
		t.Fatalf("watchlist did not survive reopen: %#v", follows)
	}
	if grouped := app.GetFollowList(int(groups[0].ID)); grouped == nil || len(*grouped) != 1 {
		t.Fatalf("group membership did not survive reopen: %#v", grouped)
	}
	note := app.GetResearchNote("600519.SH")
	if note.CoreLogic != "品牌与现金流" || note.InvalidationRisk != "渠道库存恶化" {
		t.Fatalf("research note did not survive reopen: %#v", note)
	}
	layouts, err := app.ListMultiKlineLayouts()
	if err != nil || len(layouts) != 1 || layouts[0].GridSize != 4 {
		t.Fatalf("layout did not survive reopen: %#v, %v", layouts, err)
	}
	if value := app.GetStockKingPreference("kline.lastSymbol"); value != "600519.SH" {
		t.Fatalf("preference did not survive reopen: %q", value)
	}
	if value := app.GetStockKingPreference("aiAdvice.lastSession.v1"); value != adviceSession {
		t.Fatalf("AI advice page snapshot did not survive reopen: %q", value)
	}
}

func TestEligibleQuantSymbolsReadsTheStockBasicModelTable(t *testing.T) {
	db.Init(filepath.Join(t.TempDir(), "quant-universe.db"))
	sqlDB, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sqlDB.Close() })
	if err := db.Dao.AutoMigrate(&data.StockBasic{}); err != nil {
		t.Fatal(err)
	}
	tooNew := time.Now().AddDate(0, -2, 0).Format("20060102")
	rows := []data.StockBasic{
		// A sparse duplicate from the TDX incremental feed must not replace the
		// richer Tushare metadata or appear twice in the training request.
		{TsCode: "600000.SH", Name: "浦发银行"},
		{TsCode: "600000.SH", Name: "浦发银行", Industry: "银行", ListStatus: "L", ListDate: "19991110"},
		{TsCode: "300750.SZ", Name: "宁德时代", ListStatus: "", ListDate: "20180611"},
		{TsCode: "600001.SH", Name: "退市样本", ListStatus: "L", ListDate: "19990101"},
		{TsCode: "688999.SH", Name: "新股样本", ListStatus: "L", ListDate: tooNew},
		{TsCode: "920001.BJ", Name: "北交所样本", ListStatus: "L", ListDate: "20200101"},
		{TsCode: "000001.SH", Name: "上证指数"},
		{TsCode: "510300.SH", Name: "沪深300ETF"},
		{TsCode: "159915.SZ", Name: "创业板ETF"},
		{TsCode: "113001.SH", Name: "可转债样本"},
		{TsCode: "900901.SH", Name: "B股样本"},
		{TsCode: "200002.SZ", Name: "B股样本"},
	}
	if err := db.Dao.Create(&rows).Error; err != nil {
		t.Fatal(err)
	}

	symbols, err := eligibleQuantSymbols()
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(symbols, ",") != "300750.SZ,600000.SH,920001.BJ" {
		t.Fatalf("unexpected quant universe: %#v", symbols)
	}

	trainingSymbols, metadata, err := eligibleQuantUniverse(true)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(trainingSymbols, ",") != "300750.SZ,600000.SH,600001.SH,920001.BJ" {
		t.Fatalf("unexpected historical quant universe: %#v", trainingSymbols)
	}
	if metadata["600000.SH"]["industry"] != "银行" {
		t.Fatalf("duplicate resolution discarded rich metadata: %#v", metadata["600000.SH"])
	}
}

func TestIsQuantAShareSymbolRejectsOtherExchangeSecurities(t *testing.T) {
	tests := map[string]bool{
		"600519.SH": true,
		"688981.SH": true,
		"000001.SZ": true,
		"300750.SZ": true,
		"430047.BJ": true,
		"920001.BJ": true,
		"000001.SH": false,
		"510300.SH": false,
		"159915.SZ": false,
		"113001.SH": false,
		"900901.SH": false,
		"200002.SZ": false,
		"AAPL":      false,
	}
	for symbol, expected := range tests {
		if actual := isQuantAShareSymbol(symbol); actual != expected {
			t.Errorf("isQuantAShareSymbol(%q) = %v, want %v", symbol, actual, expected)
		}
	}
}
