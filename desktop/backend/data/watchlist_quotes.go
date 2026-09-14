package data

import (
	"go-stock/backend/db"
	"gorm.io/gorm"
	"math"
	"strconv"
	"strings"
)

func validWatchQuote(q StockInfo) (float64, string, bool) {
	p, err := strconv.ParseFloat(q.Price, 64)
	stamp := strings.TrimSpace(q.Date + " " + q.Time)
	return p, stamp, err == nil && p > 0 && !math.IsNaN(p) && !math.IsInf(p, 0) && q.Date != ""
}

func dbWatchQuotesStale(codes []string) error {
	return db.Dao.Model(&FollowedStock{}).Where("stock_code IN ?", codes).Update("latest_quote_status", "stale").Error
}
func (receiver StockDataApi) updateWatchQuote(codes []string, code string, price float64, stamp string) error {
	return db.Dao.Model(&FollowedStock{}).Where("stock_code IN ? AND stock_code = ?", codes, code).Updates(map[string]any{"price": price, "latest_quote_time": stamp, "latest_quote_status": "available"}).Error
}

// Only the quote obtained by this addition may establish its immutable baseline.
func ApplySelectionQuote(dao *gorm.DB, code, generation string, q StockInfo) error {
	p, stamp, valid := validWatchQuote(q)
	updates := map[string]any{"selection_status": "unavailable"}
	if valid && normalizeStockCode(q.Code) == normalizeStockCode(code) {
		updates = map[string]any{"selection_price": p, "selection_quote_time": stamp, "selection_source": "configured_quote_feed", "selection_status": "recorded", "price": p, "latest_quote_time": stamp, "latest_quote_status": "available"}
		if strings.TrimSpace(q.Name) != "" {
			updates["name"] = q.Name
		}
	}
	return dao.Model(&FollowedStock{}).Where("stock_code = ? AND selection_id = ? AND is_del = 0 AND selection_status = ?", code, generation, "pending").Updates(updates).Error
}

// Refreshing live prices never establishes or changes selection prices.
func (receiver StockDataApi) RefreshWatchlistQuotes(groupID int) (*[]FollowedStock, error) {
	rows := receiver.GetFollowList(groupID)
	if rows == nil || len(*rows) == 0 {
		return rows, nil
	}
	codes := make([]string, 0, len(*rows))
	for _, row := range *rows {
		codes = append(codes, row.StockCode)
	}
	if err := dbWatchQuotesStale(codes); err != nil {
		return rows, err
	}
	quotes, err := receiver.GetStockCodeRealTimeData(codes...)
	if err != nil {
		return receiver.GetFollowList(groupID), err
	}
	if quotes != nil {
		for _, q := range *quotes {
			p, stamp, ok := validWatchQuote(q)
			if !ok {
				continue
			}
			code := normalizeStockCode(q.Code)
			if err := receiver.updateWatchQuote(codes, code, p, stamp); err != nil {
				return rows, err
			}
		}
	}
	return receiver.GetFollowList(groupID), nil
}

func ApplyLatestWatchQuote(dao *gorm.DB, code string, q StockInfo) error {
	p, stamp, ok := validWatchQuote(q)
	if !ok {
		return nil
	}
	return dao.Model(&FollowedStock{}).Where("stock_code = ? AND is_del = 0", code).Updates(map[string]any{"price": p, "latest_quote_time": stamp, "latest_quote_status": "available"}).Error
}
