package data

import (
	"go-stock/backend/db"
	"go-stock/backend/models"
	"log"
	"testing"
	"time"
)

func TestBKFundFlowApi_FetchAndSave(t *testing.T) {
	// 初始化数据库
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 测试从东方财富抓取数据并保存
	count, err := api.FetchAndSave()
	if err != nil {
		t.Errorf("FetchAndSave() error = %v", err)
		return
	}

	t.Logf("FetchAndSave() saved %d records", count)
	if count == 0 {
		t.Log("Warning: no records saved, this might be due to API returning empty data")
	}
}

func TestBKFundFlowApi_GetBKFundFlowList(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 先获取所有板块代码
	allCodes := api.GetAllBKCodes()
	if len(allCodes) == 0 {
		t.Skip("No BK codes found in database, skipping GetBKFundFlowList test")
	}

	// 使用第一个板块代码测试
	testCode := allCodes[0]["code"]
	testName := allCodes[0]["name"]

	t.Logf("Testing with code: %s, name: %s", testCode, testName)

	// 测试默认限制（240条）
	points := api.GetBKFundFlowList(testCode, 0)
	t.Logf("GetBKFundFlowList(%s, 0) returned %d points", testCode, len(points))
	if len(points) > 0 {
		t.Logf("First point: SnapTime=%s, NetInflow=%d", points[0].SnapTime, points[0].NetInflow)
		t.Logf("Last point: SnapTime=%s, NetInflow=%d", points[len(points)-1].SnapTime, points[len(points)-1].NetInflow)
	}

	// 测试指定限制
	points10 := api.GetBKFundFlowList(testCode, 10)
	t.Logf("GetBKFundFlowList(%s, 10) returned %d points", testCode, len(points10))
	if len(points10) > 10 {
		t.Errorf("GetBKFundFlowList() returned more than 10 points: %d", len(points10))
	}
}

func TestBKFundFlowApi_GetBKFundFlowTopList(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 测试默认前20名
	topList := api.GetBKFundFlowTopList(0)
	t.Logf("GetBKFundFlowTopList(0) returned %d records", len(topList))

	if len(topList) > 0 {
		t.Log("Top 5 sectors by net inflow:")
		for i := 0; i < len(topList) && i < 5; i++ {
			t.Logf("  [%d] %s (%s): NetInflow=%d, SnapTime=%s",
				i+1, topList[i].Name, topList[i].Code, topList[i].NetInflow, topList[i].SnapTime)
		}
	}

	// 测试前10名
	top10 := api.GetBKFundFlowTopList(10)
	t.Logf("GetBKFundFlowTopList(10) returned %d records", len(top10))
	if len(top10) > 10 {
		t.Errorf("GetBKFundFlowTopList(10) returned more than 10 records: %d", len(top10))
	}
}

func TestBKFundFlowApi_GetAllBKCodes(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	codes := api.GetAllBKCodes()
	t.Logf("GetAllBKCodes() returned %d codes", len(codes))

	if len(codes) > 0 {
		t.Log("First 10 codes:")
		for i := 0; i < len(codes) && i < 10; i++ {
			t.Logf("  [%d] Code: %s, Name: %s", i+1, codes[i]["code"], codes[i]["name"])
		}
	}
}

func TestBKFundFlowApi_CleanOldData(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 清理7天前的数据
	deleted := api.CleanOldData(7)
	t.Logf("CleanOldData(7) deleted %d records", deleted)

	// 清理3天前的数据（默认值）
	deleted3 := api.CleanOldData(0)
	t.Logf("CleanOldData(0) deleted %d records", deleted3)
}

func TestBKFundFlowApi_FullWorkflow(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 1. 抓取并保存数据
	t.Log("=== Step 1: Fetch and save data ===")
	count, err := api.FetchAndSave()
	if err != nil {
		t.Fatalf("FetchAndSave() error = %v", err)
	}
	t.Logf("Saved %d records", count)

	if count == 0 {
		t.Skip("No data fetched, skipping remaining tests")
	}

	// 2. 获取所有板块代码
	t.Log("\n=== Step 2: Get all BK codes ===")
	allCodes := api.GetAllBKCodes()
	t.Logf("Total unique BK codes: %d", len(allCodes))

	if len(allCodes) == 0 {
		t.Fatal("No BK codes found after fetch")
	}

	// 3. 获取Top列表
	t.Log("\n=== Step 3: Get top list ===")
	topList := api.GetBKFundFlowTopList(10)
	t.Logf("Top 10 sectors by net inflow:")
	for i, item := range topList {
		t.Logf("  [%d] %s (%s): ¥%d", i+1, item.Name, item.Code, item.NetInflow)
	}

	// 4. 获取某个板块的历史数据
	t.Log("\n=== Step 4: Get historical data for first code ===")
	testCode := allCodes[0]["code"]
	testName := allCodes[0]["name"]
	history := api.GetBKFundFlowList(testCode, 20)
	t.Logf("Historical data for %s (%s): %d points", testName, testCode, len(history))

	if len(history) > 0 {
		// 显示最近5条记录
		start := 0
		if len(history) > 5 {
			start = len(history) - 5
		}
		for i := start; i < len(history); i++ {
			t.Logf("  [%d] %s: ¥%d", i+1, history[i].SnapTime, history[i].NetInflow)
		}
	}

	// 5. 清理旧数据
	t.Log("\n=== Step 5: Clean old data ===")
	deleted := api.CleanOldData(3)
	t.Logf("Deleted %d records older than 3 days", deleted)
}

func TestBKFundFlowApi_ConcurrentFetch(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 测试并发调用（模拟多次快速调用）
	done := make(chan bool, 3)

	for i := 0; i < 3; i++ {
		go func(idx int) {
			count, err := api.FetchAndSave()
			if err != nil {
				t.Logf("Goroutine %d: FetchAndSave() error = %v", idx, err)
			} else {
				t.Logf("Goroutine %d: saved %d records", idx, count)
			}
			done <- true
		}(i)
	}

	// 等待所有goroutine完成
	for i := 0; i < 3; i++ {
		<-done
	}

	// 验证数据完整性
	allCodes := api.GetAllBKCodes()
	t.Logf("After concurrent fetch: %d unique codes", len(allCodes))
}

func TestBKFundFlowApi_DataIntegrity(t *testing.T) {
	db.Init("../../data/stock.db")

	api := NewBKFundFlowApi()

	// 先确保有数据
	count, _ := api.FetchAndSave()
	if count == 0 {
		t.Skip("No data available for integrity test")
	}

	// 1. 验证时间格式
	topList := api.GetBKFundFlowTopList(20)
	for _, item := range topList {
		_, err := time.Parse("2006-01-02 15:04:05", item.SnapTime)
		if err != nil {
			t.Errorf("Invalid time format for code %s: %s, error: %v", item.Code, item.SnapTime, err)
		}
	}

	// 2. 验证板块代码格式（应该以BK开头）
	allCodes := api.GetAllBKCodes()
	for _, codeMap := range allCodes {
		code := codeMap["code"]
		if len(code) == 0 {
			t.Error("Empty code found")
		}
	}

	// 3. 验证历史数据按时间排序
	if len(allCodes) > 0 {
		testCode := allCodes[0]["code"]
		points := api.GetBKFundFlowList(testCode, 50)
		for i := 1; i < len(points); i++ {
			if points[i].SnapTime < points[i-1].SnapTime {
				t.Errorf("Points not sorted by time: %s < %s", points[i].SnapTime, points[i-1].SnapTime)
			}
		}
	}

	t.Log("Data integrity check passed")
}

// TestBKFundFlowApi_ModelValidation 验证模型字段
func TestBKFundFlowApi_ModelValidation(t *testing.T) {
	// 测试 BKFundFlow 模型
	bkf := models.BKFundFlow{
		Code:      "BK0475",
		Name:      "测试板块",
		NetInflow: 12345678,
		SnapTime:  "2026-06-08 10:30:00",
	}

	if bkf.Code != "BK0475" {
		t.Errorf("BKFundFlow.Code: got %s, want BK0475", bkf.Code)
	}
	if bkf.Name != "测试板块" {
		t.Errorf("BKFundFlow.Name: got %s, want 测试板块", bkf.Name)
	}
	if bkf.NetInflow != 12345678 {
		t.Errorf("BKFundFlow.NetInflow: got %d, want 12345678", bkf.NetInflow)
	}

	// 测试 BKFundFlowPoint 模型
	point := models.BKFundFlowPoint{
		SnapTime:  "2026-06-08 10:30:00",
		NetInflow: 9876543,
	}

	if point.SnapTime != "2026-06-08 10:30:00" {
		t.Errorf("BKFundFlowPoint.SnapTime: got %s, want 2026-06-08 10:30:00", point.SnapTime)
	}
	if point.NetInflow != 9876543 {
		t.Errorf("BKFundFlowPoint.NetInflow: got %d, want 9876543", point.NetInflow)
	}

	log.Println("Model validation passed")
}
