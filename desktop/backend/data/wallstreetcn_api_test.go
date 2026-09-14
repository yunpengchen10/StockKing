package data

import (
	"fmt"
	"strings"
	"testing"
)

func TestWallstreetcnApi_GetLives(t *testing.T) {
	channels := []struct {
		code string
		name string
	}{
		{"global-channel", "全球7x24"},
		{"a-stock-channel", "A股"},
		{"us-stock-channel", "美股"},
		{"forex-channel", "外汇"},
		{"commodity-channel", "商品"},
		{"goldc-channel", "黄金"},
		{"oil-channel", "原油"},
		{"crypto-channel", "加密货币"},
	}

	api := NewWallstreetcnApi()

	for _, ch := range channels {
		t.Run(ch.name, func(t *testing.T) {
			resp := api.GetLives(ch.code, 5, "")
			if resp == nil {
				t.Logf("[%s] 接口返回nil", ch.name)
				return
			}
			if resp.Code != 20000 {
				t.Errorf("[%s] code=%d msg=%s", ch.name, resp.Code, resp.Message)
				return
			}
			if len(resp.Data.Items) == 0 {
				t.Logf("[%s] 无快讯数据", ch.name)
				return
			}

			fmt.Printf("\n=== %s快讯 (%d条) ===\n", ch.name, len(resp.Data.Items))
			for i, item := range resp.Data.Items {
				content := item.ContentText
				if content == "" {
					content = strings.ReplaceAll(item.Content, "<p>", "")
					content = strings.ReplaceAll(content, "</p>", "")
					content = strings.TrimSpace(content)
				}
				if len(content) > 80 {
					content = content[:80] + "..."
				}
				fmt.Printf("  %d. [%d] %s | score=%d calendar=%v | %s\n",
					i+1, item.ID, item.Type, item.Score, item.IsCalendar, content)
				if item.Title != "" {
					fmt.Printf("     title: %s\n", item.Title)
				}
			}

			fmt.Printf("  next_cursor: %s\n", resp.Data.NextCursor)
			fmt.Printf("  polling_cursor: %s\n", resp.Data.PollingCursor)
		})
	}
}

func TestWallstreetcnApi_GetLivesReadable(t *testing.T) {
	api := NewWallstreetcnApi()
	md := api.GetLivesReadable("global-channel", 10)
	fmt.Println(md)
}

func TestWallstreetcnApi_GetMarketReal(t *testing.T) {
	api := NewWallstreetcnApi()
	resp := api.GetMarketReal(nil, nil)
	if resp == nil {
		t.Fatal("接口返回nil")
	}
	if resp.Code != 20000 {
		t.Fatalf("code=%d msg=%s", resp.Code, resp.Message)
	}

	fmt.Println("\n=== 全球实时行情 ===")
	fmt.Printf("字段: %v\n\n", resp.Data.Fields)
	for code, values := range resp.Data.Snapshot {
		name := code
		if cnName, ok := WSCNProdCodes[code]; ok {
			name = cnName
		}
		fmt.Printf("  %s (%s): %v\n", name, code, values)
	}
}

func TestWallstreetcnApi_GetMarketRealReadable(t *testing.T) {
	api := NewWallstreetcnApi()
	md := api.GetMarketRealReadable(nil)
	fmt.Println(md)

	fmt.Println("\n--- 仅查询黄金和原油 ---")
	md2 := api.GetMarketRealReadable([]string{"XAUUSD.OTC", "USCL.OTC"})
	fmt.Println(md2)
}

func TestWallstreetcnApi_GetKline(t *testing.T) {
	api := NewWallstreetcnApi()
	resp := api.GetKline("XAUUSD.OTC", 300, 10, nil)
	if resp == nil {
		t.Fatal("接口返回nil")
	}
	if resp.Code != 20000 {
		t.Fatalf("code=%d msg=%s", resp.Code, resp.Message)
	}

	fmt.Println("\n=== 黄金5分钟K线(最近10根) ===")
	for code, candle := range resp.Data.Candle {
		fmt.Printf("  品种: %s, K线数: %d\n", code, len(candle.Lines))
		for _, line := range candle.Lines {
			fmt.Printf("    open=%.2f close=%.2f high=%.2f low=%.2f tick_at=%.0f\n",
				line[0], line[1], line[2], line[3], line[4])
		}
	}
}

func TestWallstreetcnApi_GetKlineReadable(t *testing.T) {
	api := NewWallstreetcnApi()

	fmt.Println("--- 黄金5分钟K线 ---")
	md := api.GetKlineReadable("XAUUSD.OTC", 300, 10)
	fmt.Println(md)

	fmt.Println("--- 原油日线K线 ---")
	md2 := api.GetKlineReadable("USCL.OTC", 86400, 10)
	fmt.Println(md2)

	fmt.Println("--- 美元指数1小时K线 ---")
	md3 := api.GetKlineReadable("DXY.OTC", 3600, 10)
	fmt.Println(md3)
}

func TestWallstreetcnApi_GetCalendar(t *testing.T) {
	api := NewWallstreetcnApi()
	resp := api.GetCalendar(0, 0, 10)
	if resp == nil {
		t.Fatal("接口返回nil")
	}
	if resp.Code != 20000 {
		t.Fatalf("code=%d msg=%s", resp.Code, resp.Message)
	}

	fmt.Printf("\n=== 财经日历 (%d条) ===\n", len(resp.Data.Items))
	fmt.Printf("totalCount: %d, nextCursor: %d\n\n", resp.Data.TotalCount, resp.Data.NextCursor)
	for _, item := range resp.Data.Items {
		fmt.Printf("  [%s] %s | %s | 重要性=%d | 前值=%s 预期=%s 实际=%s\n",
			item.CountryID, item.Event, item.Title, item.Importance,
			item.Previous, item.Forecast, item.Actual)
	}
}

func TestWallstreetcnApi_GetCalendarReadable(t *testing.T) {
	api := NewWallstreetcnApi()
	md := api.GetCalendarReadable(3)
	fmt.Println(md)
}
