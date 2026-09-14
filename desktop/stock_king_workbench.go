package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strings"
	"time"

	"go-stock/backend/db"
)

// InvestmentPlan keeps user-authored intent separate from actual broker fills.
type InvestmentPlan struct {
	ID           string  `json:"id"`
	AccountID    int     `json:"accountId"`
	Symbol       string  `json:"symbol"`
	Name         string  `json:"name"`
	Horizon      string  `json:"horizon"`
	EntryPrice   float64 `json:"entryPrice"`
	StopPrice    float64 `json:"stopPrice"`
	TargetPrice  float64 `json:"targetPrice"`
	Quantity     int     `json:"quantity"`
	ExpiresOn    string  `json:"expiresOn"`
	ReviewOn     string  `json:"reviewOn"`
	Thesis       string  `json:"thesis"`
	Invalidation string  `json:"invalidation"`
	Status       string  `json:"status"`
	Source       string  `json:"source"`
	CreatedAt    string  `json:"createdAt"`
	UpdatedAt    string  `json:"updatedAt"`
}

func (a *App) ListInvestmentPlans() ([]InvestmentPlan, error) {
	var rows []StockKingPreference
	if err := db.Dao.Where("key LIKE ?", "investment.plan.%").Order("updated_at desc").Find(&rows).Error; err != nil {
		return nil, err
	}
	plans := make([]InvestmentPlan, 0, len(rows))
	for _, row := range rows {
		var plan InvestmentPlan
		if err := json.Unmarshal([]byte(row.Value), &plan); err != nil {
			return nil, fmt.Errorf("交易计划记录损坏：%s", row.Key)
		}
		plans = append(plans, plan)
	}
	return plans, nil
}

func validateInvestmentPlan(plan InvestmentPlan) error {
	if plan.ID == "" || len(plan.ID) > 64 || strings.ContainsAny(plan.ID, "/\\%_ ") {
		return fmt.Errorf("无效的计划编号")
	}
	if plan.AccountID <= 0 || !isSixDigitCode(strings.Split(normalizeSymbolKey(plan.Symbol), ".")[0]) {
		return fmt.Errorf("请选择账户并填写六位A股代码")
	}
	code := normalizeSymbolKey(plan.Symbol)
	if !(strings.HasSuffix(code, ".SH") || strings.HasSuffix(code, ".SZ") || strings.HasSuffix(code, ".BJ")) {
		return fmt.Errorf("当前行动计划支持A股账户")
	}
	if plan.Horizon != "short" && plan.Horizon != "swing" && plan.Horizon != "long" {
		return fmt.Errorf("请选择短期、波段或长期计划")
	}
	for _, v := range []float64{plan.EntryPrice, plan.StopPrice, plan.TargetPrice} {
		if math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
			return fmt.Errorf("价格必须是有效非负数")
		}
	}
	if plan.EntryPrice <= 0 || plan.StopPrice <= 0 || plan.StopPrice >= plan.EntryPrice || plan.Quantity <= 0 {
		return fmt.Errorf("请输入有效数量及低于参考买价的失效价格")
	}
	if plan.TargetPrice > 0 && plan.TargetPrice <= plan.EntryPrice {
		return fmt.Errorf("目标价应高于参考买价，或留为0")
	}
	if len(plan.Thesis) > 4000 || len(plan.Invalidation) > 4000 || len(plan.Name) > 128 || len(plan.Source) > 128 {
		return fmt.Errorf("计划文字过长")
	}
	for _, value := range []string{plan.ExpiresOn, plan.ReviewOn} {
		if _, err := time.Parse("2006-01-02", value); err != nil {
			return fmt.Errorf("请填写计划有效期和复核日期")
		}
	}
	if plan.Status != "watching" && plan.Status != "holding" && plan.Status != "closed" && plan.Status != "cancelled" {
		return fmt.Errorf("无效的计划状态")
	}
	if plan.Status == "watching" {
		starMarket := strings.HasPrefix(code, "68")
		if (starMarket && plan.Quantity < 200) || (!starMarket && plan.Quantity%100 != 0) {
			return fmt.Errorf("计划数量须符合交易单位：科创板至少200股，其他A股按100股规划")
		}
	}
	return nil
}

func (a *App) SaveInvestmentPlan(plan InvestmentPlan) (InvestmentPlan, error) {
	plan.Symbol = normalizeSymbolKey(plan.Symbol)
	if err := validateInvestmentPlan(plan); err != nil {
		return InvestmentPlan{}, err
	}
	key := "investment.plan." + plan.ID
	if raw := a.GetStockKingPreference(key); raw != "" {
		var old InvestmentPlan
		if err := json.Unmarshal([]byte(raw), &old); err != nil {
			return InvestmentPlan{}, err
		}
		// Closing/reviewing a plan cannot retroactively turn a failed short trade into a long investment.
		if old.AccountID != plan.AccountID || old.Symbol != plan.Symbol || old.Horizon != plan.Horizon {
			return InvestmentPlan{}, fmt.Errorf("账户、股票和周期已锁定，请新建计划并保留原记录")
		}
		plan.CreatedAt = old.CreatedAt
	} else {
		plan.CreatedAt = time.Now().Format(time.RFC3339)
	}
	plan.UpdatedAt = time.Now().Format(time.RFC3339)
	raw, err := json.Marshal(plan)
	if err != nil {
		return InvestmentPlan{}, err
	}
	if err = a.SaveStockKingPreference(key, string(raw)); err != nil {
		return InvestmentPlan{}, err
	}
	return plan, nil
}

func (a *App) GetInvestmentAccounts() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/portfolio/accounts", nil)
}
func (a *App) CreateInvestmentAccount(name string) (map[string]any, error) {
	return a.dailyMap(http.MethodPost, "/api/v1/portfolio/accounts", map[string]any{"name": strings.TrimSpace(name), "market": "cn", "base_currency": "CNY"})
}
func (a *App) GetInvestmentSnapshot() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/portfolio/snapshot?include_realtime=true", nil)
}
func (a *App) GetInvestmentTrades(accountID int) (map[string]any, error) {
	return a.dailyMap(http.MethodGet, fmt.Sprintf("/api/v1/portfolio/trades?account_id=%d&page_size=100", accountID), nil)
}
func (a *App) RecordInvestmentTrade(payload map[string]any) (map[string]any, error) {
	return a.dailyMap(http.MethodPost, "/api/v1/portfolio/trades", payload)
}
func (a *App) RecordInvestmentCash(payload map[string]any) (map[string]any, error) {
	return a.dailyMap(http.MethodPost, "/api/v1/portfolio/cash-ledger", payload)
}
func (a *App) GetInvestmentRisk() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/portfolio/risk?include_realtime=false", nil)
}

func (a *App) RunExecutableBacktest(payload map[string]any) (map[string]any, error) {
	return a.dailyMap(http.MethodPost, "/api/v1/quant/backtests/executable", payload)
}
func (a *App) GetExecutableBacktestTemplate() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/quant/backtests/template", nil)
}
func (a *App) ListExecutableBacktests(limit int) (map[string]any, error) {
	if limit < 1 || limit > 100 {
		limit = 20
	}
	return a.dailyMap(http.MethodGet, fmt.Sprintf("/api/v1/quant/backtests/reports?limit=%d", limit), nil)
}
func (a *App) GetExecutableBacktestReport(reportID string) (map[string]any, error) {
	if len(reportID) != 64 {
		return nil, fmt.Errorf("无效报告编号")
	}
	if _, err := hex.DecodeString(reportID); err != nil {
		return nil, fmt.Errorf("无效报告编号")
	}
	return a.dailyMap(http.MethodGet, "/api/v1/quant/backtests/reports/"+reportID, nil)
}
func (a *App) StartQuantTrainingScoped(symbols []string, objectives []string) (map[string]any, error) {
	if len(symbols) < 20 || len(symbols) > 200 {
		return nil, fmt.Errorf("请明确选择20–200只股票；范围较小的训练不能代表全市场表现")
	}
	normalized := make([]string, 0, len(symbols))
	seen := map[string]bool{}
	for _, symbol := range symbols {
		code := normalizeSymbolKey(symbol)
		if !isSixDigitCode(strings.Split(code, ".")[0]) || !(strings.HasSuffix(code, ".SH") || strings.HasSuffix(code, ".SZ") || strings.HasSuffix(code, ".BJ")) {
			return nil, fmt.Errorf("训练仅支持有效A股代码：%s", symbol)
		}
		if !seen[code] {
			normalized = append(normalized, code)
			seen[code] = true
		}
	}
	if len(normalized) < 20 {
		return nil, fmt.Errorf("至少需要20只不同股票")
	}
	if len(objectives) == 0 {
		return nil, fmt.Errorf("请选择需要训练的模型目标")
	}
	for _, objective := range objectives {
		if objective != "conservative" && objective != "regular" && objective != "aggressive" {
			return nil, fmt.Errorf("未知模型目标：%s", objective)
		}
	}
	return a.dailyMap(http.MethodPost, "/api/v1/quant/training/tasks", map[string]any{"symbols": normalized, "horizons": []int{1, 5, 20}, "objectives": objectives, "train_master": false, "legacy_forecasts": false})
}
