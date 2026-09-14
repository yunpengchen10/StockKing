package data

import (
	"go-stock/backend/db"
	"log"
	"math"
	"testing"
)

func TestSaveStockChangesWithDedup(t *testing.T) {
	db.Init("../../data/stock.db")

	service := NewStockChangesApi()
	data := service.GetAllStockChangesWithPaging(1000)
	log.Println(len(data.Data))
	historyService := NewStockChangeHistoryService()
	count, err := historyService.SaveStockChangesWithDedup(data.Data)
	log.Println(count, err)
	if err != nil {
		t.Errorf("SaveStockChangesWithDedup() error = %v", err)
	}
	t.Logf("SaveStockChangesWithDedup() count = %d", count)

}

func TestStockChangesApi_GetStockChanges(t *testing.T) {
	api := NewStockChangesApi()

	tests := []struct {
		name        string
		changeTypes []int
		pageSize    int
	}{
		{
			name:        "默认查询",
			changeTypes: nil,
			pageSize:    10,
		},
		{
			name:        "查询火箭发射和快速反弹",
			changeTypes: []int{8201, 8202},
			pageSize:    20,
		},
		{
			name:        "查询封涨停板和封跌停板",
			changeTypes: []int{4, 8},
			pageSize:    15,
		},
		{
			name:        "查询大笔买入和大笔卖出",
			changeTypes: []int{8193, 8194},
			pageSize:    10,
		},
		{
			name:        "查询60日新高和60日新低",
			changeTypes: []int{8213, 8214},
			pageSize:    10,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := api.GetStockChanges(tt.changeTypes, 0, tt.pageSize)
			if result == nil {
				t.Errorf("GetStockChanges() returned nil")
				return
			}
			t.Logf("TotalCount: %d, DataCount: %d", result.TotalCount, len(result.Data))
			for i, item := range result.Data {
				if i >= 3 {
					break
				}
				t.Logf("  [%d] %s %s %s 价格:%.2f 涨幅:%.2f%% 数量:%d 金额:%.2f",
					i+1, item.Time, item.Code, item.Name, item.Price, item.ChangeRate, item.Volume, item.Amount)
			}
		})
	}
}

func TestStockChangesApi_GetStockChangesReadable(t *testing.T) {
	api := NewStockChangesApi()

	result := api.GetStockChangesReadable([]int{8201, 8204}, 0, 5)
	if result == "" {
		t.Error("GetStockChangesReadable() returned empty string")
	}
	t.Logf("Result:\n%s", result)
}

func TestParseItemData(t *testing.T) {
	tests := []struct {
		name        string
		data        string
		changeType  int
		expectPrice float64
		expectRate  float64
		expectVol   int64
	}{
		{
			name:        "火箭发射格式-涨跌幅,现价",
			data:        "0.052345,15.68",
			changeType:  8201,
			expectPrice: 15.68,
			expectRate:  5.2345,
			expectVol:   0,
		},
		{
			name:        "加速下跌格式-涨跌幅,现价",
			data:        "-0.031234,10.25",
			changeType:  8204,
			expectPrice: 10.25,
			expectRate:  -3.1234,
			expectVol:   0,
		},
		{
			name:        "大笔买入格式-成交量,现价,涨跌幅,成交额",
			data:        "15204345,7.57,-0.028947,11500000.00",
			changeType:  8193,
			expectPrice: 7.57,
			expectRate:  -2.8947,
			expectVol:   15204345,
		},
		{
			name:        "封涨停板格式-现价,封单量,?,涨跌幅",
			data:        "12.50,5000000,0,0.10",
			changeType:  4,
			expectPrice: 12.50,
			expectRate:  10.0,
			expectVol:   5000000,
		},
		{
			name:        "打开涨停板格式-现价,涨跌幅",
			data:        "11.80,0.095",
			changeType:  16,
			expectPrice: 11.80,
			expectRate:  9.5,
			expectVol:   0,
		},
		{
			name:        "有大买盘格式-数量,现价,涨跌幅,金额",
			data:        "100000,25.30,0.05,2530000.00",
			changeType:  64,
			expectPrice: 25.30,
			expectRate:  5.0,
			expectVol:   100000,
		},
		{
			name:        "60日新高格式-现价,现价,涨跌幅",
			data:        "35.20,35.20,0.08",
			changeType:  8213,
			expectPrice: 35.20,
			expectRate:  8.0,
			expectVol:   0,
		},
		{
			name:        "60日大幅上涨格式-涨跌幅,现价,涨跌幅",
			data:        "0.15,45.60,0.15",
			changeType:  8215,
			expectPrice: 45.60,
			expectRate:  15.0,
			expectVol:   0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			item := &StockChangeItem{}
			parseItemData(tt.data, item, tt.changeType)

			if math.Abs(item.Price-tt.expectPrice) > 0.001 {
				t.Errorf("Price: got %.2f, want %.2f", item.Price, tt.expectPrice)
			}
			if math.Abs(item.ChangeRate-tt.expectRate) > 0.001 {
				t.Errorf("ChangeRate: got %.4f, want %.4f", item.ChangeRate, tt.expectRate)
			}
			if item.Volume != tt.expectVol {
				t.Errorf("Volume: got %d, want %d", item.Volume, tt.expectVol)
			}
		})
	}
}

func TestFormatTime(t *testing.T) {
	tests := []struct {
		input    int
		expected string
	}{
		{134221, "13:42:21"},
		{93015, "09:30:15"},
		{150000, "15:00:00"},
	}

	for _, tt := range tests {
		result := formatTime(tt.input)
		if result != tt.expected {
			t.Errorf("formatTime(%d) = %s, want %s", tt.input, result, tt.expected)
		}
	}
}

func TestGetChangeTypeName(t *testing.T) {
	tests := []struct {
		input    int
		expected string
	}{
		{8201, "火箭发射"},
		{8202, "快速反弹"},
		{8193, "大笔买入"},
		{4, "封涨停板"},
		{8, "封跌停板"},
		{16, "打开涨停板"},
		{32, "打开跌停板"},
		{64, "有大买盘"},
		{128, "有大卖盘"},
		{99999, "类型99999"},
	}

	for _, tt := range tests {
		result := getChangeTypeName(tt.input)
		if result != tt.expected {
			t.Errorf("getChangeTypeName(%d) = %s, want %s", tt.input, result, tt.expected)
		}
	}
}
