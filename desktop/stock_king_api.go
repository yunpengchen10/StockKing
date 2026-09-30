package main

import (
	"context"
	"encoding/json"
	"fmt"
	"go-stock/backend/data"
	"go-stock/backend/db"
	"go-stock/backend/logger"
	"go-stock/backend/models"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"gorm.io/gorm"
)

// SymbolRef is the shared security identity used by Go, Vue, and the Daily
// engine. Market and board are metadata, never inferred by the UI alone.
type SymbolRef struct {
	Code     string `json:"code"`
	Name     string `json:"name,omitempty"`
	Market   string `json:"market"`
	Exchange string `json:"exchange,omitempty"`
	Board    string `json:"board,omitempty"`
	Currency string `json:"currency,omitempty"`
}

type ModelMetrics struct {
	ModelName       string  `json:"modelName"`
	ModelVersion    string  `json:"modelVersion"`
	Horizon         int     `json:"horizon"`
	RankIC          float64 `json:"rankIc"`
	PositiveWindows int     `json:"positiveWindows"`
	TotalWindows    int     `json:"totalWindows"`
	BrierScore      float64 `json:"brierScore"`
	BaselineBrier   float64 `json:"baselineBrier"`
	NetSharpe       float64 `json:"netSharpe"`
	Coverage        float64 `json:"coverage"`
	Qualified       bool    `json:"qualified"`
	Reason          string  `json:"reason,omitempty"`
}

type ForecastSnapshot struct {
	Symbol               SymbolRef          `json:"symbol"`
	Horizon              int                `json:"horizon"`
	AsOfDate             string             `json:"asOfDate"`
	ProbabilityUp        float64            `json:"probabilityUp"`
	ProbabilityFlat      float64            `json:"probabilityFlat"`
	ProbabilityDown      float64            `json:"probabilityDown"`
	ExpectedExcessReturn float64            `json:"expectedExcessReturn"`
	IntervalLow          float64            `json:"intervalLow"`
	IntervalHigh         float64            `json:"intervalHigh"`
	ModelVersion         string             `json:"modelVersion"`
	KeyFeatures          map[string]float64 `json:"keyFeatures,omitempty"`
	Metrics              ModelMetrics       `json:"metrics"`
	Available            bool               `json:"available"`
	UnavailableReason    string             `json:"unavailableReason,omitempty"`
}

type DailyPick struct {
	Symbol             SymbolRef          `json:"symbol"`
	BoardGroup         string             `json:"boardGroup"`
	Rank               int                `json:"rank"`
	Score              float64            `json:"score"`
	Strategy           string             `json:"strategy"`
	RecommendationTier string             `json:"recommendationTier"`
	TierScore          float64            `json:"tierScore"`
	ScoreMeaning       string             `json:"scoreMeaning,omitempty"`
	TierReasons        []string           `json:"tierReasons,omitempty"`
	PotentialScore     float64            `json:"potentialScore,omitempty"`
	PotentialHorizon   string             `json:"potentialHorizon,omitempty"`
	PotentialReasons   []string           `json:"potentialReasons,omitempty"`
	ScreeningSource    string             `json:"screeningSource,omitempty"`
	RiskDecision       string             `json:"riskDecision,omitempty"`
	DegradedReasons    []string           `json:"degradedReasons,omitempty"`
	Forecasts          []ForecastSnapshot `json:"forecasts,omitempty"`
	ModelStatus        string             `json:"modelStatus,omitempty"`
	ModelVersion       string             `json:"modelVersion,omitempty"`
	TrainedThrough     string             `json:"trainedThrough,omitempty"`
	CalibratedThrough  string             `json:"calibratedThrough,omitempty"`
	ConfidenceGrade    string             `json:"confidenceGrade,omitempty"`
	LearnedWeights     map[string]float64 `json:"learnedModelWeights,omitempty"`
	ReturnLower20D     float64            `json:"returnLowerBound20d,omitempty"`
	ReturnLower60D     float64            `json:"returnLowerBound60d,omitempty"`
	PredictedVol20D    float64            `json:"predictedVolatility20d,omitempty"`
	DrawdownQ20D       float64            `json:"drawdownQuantile20d,omitempty"`
	ExcessRank5D       float64            `json:"excessRank5d,omitempty"`
	ExcessRank20D      float64            `json:"excessRank20d,omitempty"`
	TouchProbability1D float64            `json:"touchProbability1d,omitempty"`
	TouchProbability3D float64            `json:"touchProbability3d,omitempty"`
	BaseRate3D         float64            `json:"baseRate3d,omitempty"`
	ProbabilityLift    float64            `json:"probabilityLift,omitempty"`
}

// ResearchNote is intentionally owned by the user. AI endpoints receive a
// read-only copy; only SaveResearchNote can alter it.
type ResearchNote struct {
	ID               uint      `json:"id" gorm:"primaryKey"`
	SymbolCode       string    `json:"symbolCode" gorm:"uniqueIndex;size:32;not null"`
	SymbolName       string    `json:"symbolName" gorm:"size:128"`
	CoreLogic        string    `json:"coreLogic" gorm:"type:text"`
	ObservationPlan  string    `json:"observationPlan" gorm:"type:text"`
	InvalidationRisk string    `json:"invalidationRisk" gorm:"type:text"`
	CreatedAt        time.Time `json:"createdAt"`
	UpdatedAt        time.Time `json:"updatedAt"`
}

type MultiKlineLayout struct {
	ID             uint      `json:"id" gorm:"primaryKey"`
	Name           string    `json:"name" gorm:"uniqueIndex;size:80;not null"`
	GridSize       int       `json:"gridSize"`
	SyncPeriod     bool      `json:"syncPeriod"`
	SyncCrosshair  bool      `json:"syncCrosshair"`
	Period         string    `json:"period" gorm:"size:16"`
	SymbolsJSON    string    `json:"symbolsJson" gorm:"type:text"`
	IndicatorsJSON string    `json:"indicatorsJson" gorm:"type:text"`
	IsDefault      bool      `json:"isDefault"`
	CreatedAt      time.Time `json:"createdAt"`
	UpdatedAt      time.Time `json:"updatedAt"`
}

type StockKingPreference struct {
	Key       string    `json:"key" gorm:"primaryKey;size:80"`
	Value     string    `json:"value" gorm:"type:text"`
	UpdatedAt time.Time `json:"updatedAt"`
}

func (a *App) GetEngineStatus() EngineStatus {
	if a.sidecar == nil {
		return EngineStatus{State: "unavailable", Message: "Daily engine manager is unavailable"}
	}
	return a.sidecar.Status()
}

func (a *App) GetResearchNote(symbolCode string) ResearchNote {
	var note ResearchNote
	db.Dao.Where("symbol_code = ?", normalizeSymbolKey(symbolCode)).First(&note)
	return note
}

func (a *App) SaveResearchNote(note ResearchNote) (ResearchNote, error) {
	note.SymbolCode = normalizeSymbolKey(note.SymbolCode)
	if note.SymbolCode == "" {
		return ResearchNote{}, fmt.Errorf("symbol code is required")
	}
	var existing ResearchNote
	err := db.Dao.Where("symbol_code = ?", note.SymbolCode).First(&existing).Error
	if err != nil && err != gorm.ErrRecordNotFound {
		return ResearchNote{}, err
	}
	if existing.ID != 0 {
		note.ID = existing.ID
		note.CreatedAt = existing.CreatedAt
	}
	if err := db.Dao.Save(&note).Error; err != nil {
		return ResearchNote{}, err
	}
	return note, nil
}

func (a *App) ListMultiKlineLayouts() ([]MultiKlineLayout, error) {
	var layouts []MultiKlineLayout
	err := db.Dao.Order("is_default desc, updated_at desc").Find(&layouts).Error
	return layouts, err
}

func (a *App) SaveMultiKlineLayout(layout MultiKlineLayout) (MultiKlineLayout, error) {
	layout.Name = strings.TrimSpace(layout.Name)
	if layout.Name == "" {
		return MultiKlineLayout{}, fmt.Errorf("layout name is required")
	}
	if layout.GridSize != 1 && layout.GridSize != 2 && layout.GridSize != 4 && layout.GridSize != 6 && layout.GridSize != 9 {
		return MultiKlineLayout{}, fmt.Errorf("grid size must be 1, 2, 4, 6, or 9")
	}
	if layout.IsDefault {
		if err := db.Dao.Model(&MultiKlineLayout{}).Where("is_default = ?", true).Update("is_default", false).Error; err != nil {
			return MultiKlineLayout{}, err
		}
	}
	var existing MultiKlineLayout
	if layout.ID == 0 {
		db.Dao.Where("name = ?", layout.Name).First(&existing)
		if existing.ID != 0 {
			layout.ID = existing.ID
			layout.CreatedAt = existing.CreatedAt
		}
	}
	if err := db.Dao.Save(&layout).Error; err != nil {
		return MultiKlineLayout{}, err
	}
	return layout, nil
}

func (a *App) DeleteMultiKlineLayout(id uint) error {
	if id == 0 {
		return fmt.Errorf("layout id is required")
	}
	return db.Dao.Delete(&MultiKlineLayout{}, id).Error
}

func (a *App) GetStockKingPreference(key string) string {
	var preference StockKingPreference
	db.Dao.Where("key = ?", strings.TrimSpace(key)).First(&preference)
	return preference.Value
}

func (a *App) SaveStockKingPreference(key, value string) error {
	key = strings.TrimSpace(key)
	if key == "" {
		return fmt.Errorf("preference key is required")
	}
	return db.Dao.Save(&StockKingPreference{Key: key, Value: value}).Error
}

func (a *App) GetScreeningStatus() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/screening/status", nil)
}

func (a *App) GetScreeningStrategies() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/screening/strategies", nil)
}

func (a *App) StartScreeningTask(strategy string, maxResults int, variantSeed string) (map[string]any, error) {
	if maxResults < 1 {
		maxResults = 20
	}
	if maxResults > 100 {
		maxResults = 100
	}
	return a.dailyMap(http.MethodPost, "/api/v1/screening/screen/tasks", map[string]any{
		"market": "cn", "strategy": strings.TrimSpace(strategy),
		"max_results": maxResults, "variant_seed": strings.TrimSpace(variantSeed),
	})
}

func (a *App) GetScreeningTask(taskID string) (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/screening/screen/tasks/"+url.PathEscape(strings.TrimSpace(taskID)), nil)
}

func (a *App) GetScreeningHistory(limit int) (map[string]any, error) {
	if limit < 1 || limit > 100 {
		limit = 20
	}
	return a.dailyMap(http.MethodGet, fmt.Sprintf("/api/v1/screening/history?limit=%d", limit), nil)
}

func (a *App) GetKingPicks(maxPerBoard int, force bool) (map[string]any, error) {
	if maxPerBoard < 5 {
		maxPerBoard = 5
	}
	if maxPerBoard > 50 {
		maxPerBoard = 50
	}
	result, err := a.dailyMap(http.MethodPost, "/api/v1/stock-king/picks", map[string]any{
		"max_per_board": maxPerBoard,
		"force":         force,
		"scan_slot":     "live",
		"top_n":         5,
	})
	if err != nil {
		return nil, err
	}
	// V1.1 signals are persisted by the engine before this response. They must
	// not be copied into the legacy AI recommendation/transaction history.
	return result, nil
}

// StartKingPicksRefresh starts only the local picks scan. The short request
// returns a task ID; polling never holds a Wails call open for the full scan.
func (a *App) StartKingPicksRefresh(maxPerBoard int) (map[string]any, error) {
	if maxPerBoard < 5 {
		maxPerBoard = 5
	}
	if maxPerBoard > 50 {
		maxPerBoard = 50
	}
	return a.kingPicksControl(http.MethodPost, "/api/v1/stock-king/picks/refresh", map[string]any{
		"max_per_board": maxPerBoard, "force": true, "scan_slot": "live", "top_n": 5, "official": false,
	})
}

func (a *App) GetKingPicksRefreshTask(taskID string) (map[string]any, error) {
	taskID = strings.TrimSpace(taskID)
	if taskID == "" {
		return nil, fmt.Errorf("本地精选刷新任务编号为空")
	}
	return a.kingPicksControl(http.MethodGet, "/api/v1/stock-king/picks/refresh/tasks/"+url.PathEscape(taskID), nil)
}

// GetDisplayedKingPicks reads the durable display snapshot. Scheduled scans
// cannot overwrite it, and this read never starts a new scan.
func (a *App) GetDisplayedKingPicks() (map[string]any, error) {
	return a.kingPicksControl(http.MethodGet, "/api/v1/stock-king/picks/display", nil)
}

func (a *App) kingPicksControl(method, path string, input any) (map[string]any, error) {
	if a.sidecar == nil {
		return nil, fmt.Errorf("Daily engine manager is unavailable")
	}
	parent := a.ctx
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, 15*time.Second)
	defer cancel()
	var output map[string]any
	if err := a.sidecar.Request(ctx, method, path, input, &output); err != nil {
		return nil, err
	}
	return output, nil
}

// GetLatestKingPicks returns the last persisted adaptive result without starting
// another market scan. It is used when the user comes back to the page so the
// previous recommendation remains visible immediately.
func (a *App) GetLatestKingPicks(scanSlot string) (map[string]any, error) {
	slot := strings.ToLower(strings.TrimSpace(scanSlot))
	allowed := map[string]bool{
		"live": true, "0920": true, "0922": true, "0925": true, "0940": true, "0955": true, "1030": true,
		"1455": true, "review": true, "weekly": true,
	}
	if !allowed[slot] {
		slot = "live"
	}
	row, err := a.dailyMap(http.MethodGet, "/api/v1/stock-king/picks/latest?scan_slot="+url.QueryEscape(slot), nil)
	if err != nil {
		return nil, err
	}
	result := stockKingAnyMap(row["result"])
	if len(result) == 0 {
		return nil, fmt.Errorf("latest King picks result is empty")
	}
	result["storedRunId"] = stockKingMapString(row, "run_id", "runId")
	result["storedCreatedAt"] = stockKingMapString(row, "created_at", "createdAt")
	return result, nil
}

// GetKingPicksHistory returns persisted snapshots including their candidate
// lists. Reading history never starts a live scan or writes a training sample.
func (a *App) GetKingPicksHistory(limit int) (map[string]any, error) {
	if limit < 1 || limit > 100 {
		limit = 30
	}
	return a.dailyMap(http.MethodGet, fmt.Sprintf("/api/v1/stock-king/picks/history?limit=%d", limit), nil)
}

func (a *App) GetStockKingRecommendationHistory(date, symbol, version string) (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/stock-king/picks/records?"+url.Values{"date": {date}, "symbol": {symbol}, "version": {version}}.Encode(), nil)
}
func (a *App) GetStockKingDelayedReviews(date, symbol, version string) (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/stock-king/picks/reviews?"+url.Values{"date": {date}, "symbol": {symbol}, "version": {version}}.Encode(), nil)
}
func (a *App) GetStockKingLearningState() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/stock-king/picks/learning", nil)
}

func syncKingPicksToRecommendations(result map[string]any) (int, error) {
	generatedAt := time.Now()
	if value := stockKingMapString(result, "generatedAt", "generated_at"); value != "" {
		if parsed, err := time.Parse(time.RFC3339, value); err == nil {
			generatedAt = parsed.Local()
		}
	}
	dayStart := time.Date(generatedAt.Year(), generatedAt.Month(), generatedAt.Day(), 0, 0, 0, 0, generatedAt.Location())
	dayEnd := dayStart.Add(24*time.Hour - time.Nanosecond)
	count := 0
	seen := map[string]bool{}
	for _, group := range stockKingRecommendationGroups(result) {
		values := group.values
		for _, value := range values {
			pick := stockKingAnyMap(value)
			symbol := stockKingAnyMap(pick["symbol"])
			code := normalizeSymbolKey(stockKingMapString(symbol, "code"))
			if code == "" {
				code = normalizeSymbolKey(stockKingMapString(pick, "code", "stockCode", "stock_code"))
			}
			if code == "" {
				continue
			}
			seenKey := group.scanSlot + ":" + code
			if seen[seenKey] {
				continue
			}
			seen[seenKey] = true
			name := stockKingMapString(symbol, "name")
			if name == "" {
				name = stockKingMapString(pick, "name", "stockName", "stock_name")
			}
			rank := stockKingMapInt(pick, "rank")
			score := stockKingMapFloat(pick, "nls", "tierScore", "tier_score", "score", "finalScore", "final_score")
			potentialScore := stockKingMapFloat(pick, "potentialScore", "potential_score")
			source := stockKingMapString(pick, "screeningSource", "screening_source", "strategy")
			forecast := stockKingMapString(pick, "forecastSummary", "forecast_summary")
			reasonParts := stockKingNonEmpty(source)
			tierReasons := stockKingMapStringSlice(pick, "tierReasons", "tier_reasons")
			potentialReasons := stockKingMapStringSlice(pick, "potentialReasons", "potential_reasons")
			if len(tierReasons) > 0 {
				reasonParts = append(reasonParts, "入选依据："+strings.Join(tierReasons, "；"))
			}
			if len(potentialReasons) > 0 {
				reasonParts = append(reasonParts, "潜力证据："+strings.Join(potentialReasons, "；"))
			}
			if forecast != "" {
				reasonParts = append(reasonParts, "量化预测："+forecast)
			}
			if meaning := stockKingMapString(pick, "scoreMeaning", "score_meaning"); meaning != "" {
				reasonParts = append(reasonParts, "分数含义："+meaning)
			}
			if group.adaptive {
				if model := stockKingMapString(pick, "modelBranch", "model", "model_branch"); model != "" {
					reasonParts = append(reasonParts, "自适应分支："+model)
				}
				if upgrade := stockKingMapStringSlice(pick, "upgradeConditions", "triggers"); len(upgrade) > 0 {
					reasonParts = append(reasonParts, "升级条件："+strings.Join(upgrade, "；"))
				}
			}
			modelStatus := stockKingMapString(pick, "modelStatus", "model_status")
			modelVersion := stockKingMapString(pick, "modelVersion", "model_version")
			if group.adaptive && (modelVersion == "king-daily-rules-20260908" || strings.HasPrefix(modelVersion, "king-local-")) {
				// New observations stay in the audited engine ledger. The
				// legacy table treats reference prices as return baselines.
				continue
			}
			if modelStatus != "" {
				modelEvidence := "模型状态：" + modelStatus
				if modelVersion != "" {
					modelEvidence += "（" + modelVersion + "）"
				}
				reasonParts = append(reasonParts, modelEvidence)
			}
			if group.tier == "aggressive" && modelStatus == "qualified" {
				p1 := stockKingMapFloat(pick, "touchProbability1d", "touch_probability_1d")
				p3 := stockKingMapFloat(pick, "touchProbability3d", "touch_probability_3d")
				base := stockKingMapFloat(pick, "baseRate3d", "base_rate_3d")
				reasonParts = append(reasonParts, fmt.Sprintf("首次触板概率：1日 %.2f%% / 3日 %.2f%%；板块基准 %.2f%%", p1*100, p3*100, base*100))
			}
			risk := stockKingMapString(pick, "riskDecision", "risk_decision")
			if group.adaptive {
				risks := stockKingMapStringSlice(pick, "risks")
				invalidations := stockKingMapStringSlice(pick, "invalidationConditions", "invalidations")
				risk = strings.Join(stockKingNonEmpty(strings.Join(risks, "；"), "失效条件："+strings.Join(invalidations, "；")), "；")
			}
			degraded := stockKingMapStringSlice(pick, "degradedReasons", "degraded_reasons")
			degraded = append(degraded, stockKingMapStringSlice(pick, "potentialDegradedReasons", "potential_degraded_reasons")...)
			if len(degraded) > 0 {
				risk = strings.Trim(strings.Join(stockKingNonEmpty(risk, "数据降级："+strings.Join(degraded, "；")), "；"), "；")
			}
			rating := fmt.Sprintf("第%d名 / %.2f分", rank, score)
			if group.tier == "aggressive" && potentialScore > 0 {
				rating = fmt.Sprintf("第%d名 / 激进百分位 %.2f分", rank, potentialScore)
			}
			if rank <= 0 {
				rating = fmt.Sprintf("%.2f分", score)
			}
			stockPrice := stockKingMapString(pick, "referencePrice", "reference_price", "stockPrice", "stock_price", "price", "close")
			modelName := "King 每日精选"
			if group.adaptive {
				modelName = "King 自适应精选"
			}
			userPrompt := fmt.Sprintf("生成日期 %s，扫描时点 %s，档位 %s，板块 %s", generatedAt.Format("2006-01-02"), group.scanSlot, group.tierLabel, group.boardLabel)
			recommendation := models.AiRecommendStocks{
				DataTime:        &generatedAt,
				ModelName:       modelName,
				Rating:          rating,
				StockCode:       code,
				StockName:       name,
				BkCode:          stockKingMapString(pick, "boardGroup", "board_group"),
				BkName:          stockKingMapString(pick, "industry"),
				StockPrice:      stockPrice,
				StockClosePrice: stockPrice,
				RecommendReason: strings.Join(reasonParts, "；"),
				RiskRemarks:     risk,
				Remarks:         "来源：King 每日精选 · " + group.tierLabel + " · " + group.boardLabel + " · " + group.scanSlot,
				SystemPrompt:    "Stock King 全市场筛选、风险覆盖、历史先验与可审计自适应模型",
				UserPrompt:      userPrompt,
			}
			var existing models.AiRecommendStocks
			err := db.Dao.Where("model_name = ? AND stock_code = ? AND user_prompt = ? AND data_time BETWEEN ? AND ?", recommendation.ModelName, code, userPrompt, dayStart, dayEnd).First(&existing).Error
			if err == nil {
				recommendation.Model = existing.Model
				recommendation.EnableAlert = existing.EnableAlert
				if err := db.Dao.Save(&recommendation).Error; err != nil {
					return count, err
				}
			} else if err == gorm.ErrRecordNotFound {
				if err := db.Dao.Create(&recommendation).Error; err != nil {
					return count, err
				}
			} else {
				return count, err
			}
			count++
		}
	}
	return count, nil
}

type stockKingRecommendationGroup struct {
	tier       string
	tierLabel  string
	boardLabel string
	scanSlot   string
	adaptive   bool
	values     []any
}

func stockKingRecommendationGroups(result map[string]any) []stockKingRecommendationGroup {
	tierLabels := map[string]string{
		"conservative": "保守推荐",
		"regular":      "常规推荐",
		"aggressive":   "激进推荐",
	}
	boardKeys := []struct {
		keys  []string
		label string
	}{
		{keys: []string{"nonKeChuang", "non_kechuang"}, label: "非科创板"},
		{keys: []string{"kechuang"}, label: "科创板"},
	}
	groups := make([]stockKingRecommendationGroup, 0, 7)
	adaptive := stockKingAnyMap(result["adaptive"])
	if values, ok := adaptive["candidates"].([]any); ok {
		scanSlot := stockKingMapString(adaptive, "scanSlot", "scan_slot")
		if scanSlot == "" {
			scanSlot = "live"
		}
		groups = append(groups, stockKingRecommendationGroup{
			tier: "adaptive", tierLabel: "自适应精选", boardLabel: "A股",
			scanSlot: scanSlot, adaptive: true, values: values,
		})
	}
	tiers := stockKingAnyMap(result["tiers"])
	if len(tiers) > 0 {
		for _, tier := range []string{"conservative", "regular", "aggressive"} {
			payload := stockKingAnyMap(tiers[tier])
			for _, board := range boardKeys {
				for _, key := range board.keys {
					if values, ok := payload[key].([]any); ok {
						groups = append(groups, stockKingRecommendationGroup{
							tier: tier, tierLabel: tierLabels[tier], boardLabel: board.label, scanSlot: "classic", values: values,
						})
						break
					}
				}
			}
		}
		return groups
	}

	// Schema v1 compatibility: the two top-level boards are the regular tier.
	for _, board := range boardKeys {
		for _, key := range board.keys {
			if values, ok := result[key].([]any); ok {
				groups = append(groups, stockKingRecommendationGroup{
					tier: "regular", tierLabel: tierLabels["regular"], boardLabel: board.label, scanSlot: "classic", values: values,
				})
				break
			}
		}
	}
	return groups
}

func stockKingAnyMap(value any) map[string]any {
	if mapped, ok := value.(map[string]any); ok {
		return mapped
	}
	return map[string]any{}
}

func stockKingMapString(value map[string]any, keys ...string) string {
	for _, key := range keys {
		if found, ok := value[key]; ok && found != nil {
			text := strings.TrimSpace(fmt.Sprint(found))
			if text != "" && text != "<nil>" {
				return text
			}
		}
	}
	return ""
}

func stockKingMapFloat(value map[string]any, keys ...string) float64 {
	text := stockKingMapString(value, keys...)
	parsed, _ := strconv.ParseFloat(text, 64)
	return parsed
}

func stockKingMapInt(value map[string]any, keys ...string) int {
	return int(math.Round(stockKingMapFloat(value, keys...)))
}

func stockKingMapStringSlice(value map[string]any, keys ...string) []string {
	for _, key := range keys {
		if raw, ok := value[key].([]string); ok {
			return append([]string(nil), raw...)
		}
		if raw, ok := value[key].([]any); ok {
			result := make([]string, 0, len(raw))
			for _, item := range raw {
				if text := strings.TrimSpace(fmt.Sprint(item)); text != "" {
					result = append(result, text)
				}
			}
			return result
		}
	}
	return nil
}

func stockKingNonEmpty(values ...string) []string {
	result := make([]string, 0, len(values))
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			result = append(result, value)
		}
	}
	return result
}

func (a *App) GetForecast(symbolCode string, horizon int) (map[string]any, error) {
	if horizon != 1 && horizon != 5 && horizon != 20 {
		return nil, fmt.Errorf("horizon must be 1, 5, or 20 trading days")
	}
	path := fmt.Sprintf("/api/v1/quant/forecasts/%s?horizon=%d", url.PathEscape(normalizeSymbolKey(symbolCode)), horizon)
	return a.dailyMap(http.MethodGet, path, nil)
}

func (a *App) GetAIAdvice(symbolCode string) (map[string]any, error) {
	advice, err := a.getStockKingAdviceEvidence(symbolCode)
	if err != nil {
		return nil, err
	}
	advice["llmExplanationStatus"] = "manual_review_required"
	return advice, nil
}

// getStockKingAdviceEvidence never calls a language model. The workspace can
// therefore export evidence or compare an imported report without API usage.
func (a *App) getStockKingAdviceEvidence(symbolCode string) (map[string]any, error) {
	note := a.GetResearchNote(symbolCode)
	technical := a.buildGoTechnicalSnapshot(symbolCode)
	advice, err := a.dailyMap(http.MethodPost, "/api/v1/stock-king/advice", map[string]any{
		"symbol_code": normalizeSymbolKey(symbolCode),
		"research_note": map[string]string{
			"core_logic":        note.CoreLogic,
			"observation_plan":  note.ObservationPlan,
			"invalidation_risk": note.InvalidationRisk,
		},
		"technical_snapshot": technical,
	})
	if err != nil {
		return nil, err
	}
	return advice, nil
}

func (a *App) SaveStockKingAIAdvice(symbolCode, symbolName, content, modelName string) (models.AIResponseResult, error) {
	code := normalizeSymbolKey(symbolCode)
	if code == "" {
		return models.AIResponseResult{}, fmt.Errorf("stock code is required")
	}
	content = strings.TrimSpace(content)
	if content == "" {
		return models.AIResponseResult{}, fmt.Errorf("analysis content is required")
	}
	if strings.TrimSpace(modelName) == "" {
		modelName = "Stock King 结构化研究"
	}
	record := models.AIResponseResult{
		ChatId:    fmt.Sprintf("stock-king-advice-%d", time.Now().UnixNano()),
		ModelName: modelName,
		StockCode: code,
		StockName: strings.TrimSpace(symbolName),
		Question:  "Stock King 结构化研究建议",
		Content:   content,
	}
	if err := db.Dao.Create(&record).Error; err != nil {
		return models.AIResponseResult{}, err
	}
	return record, nil
}

func (a *App) GetStockKingAIAdviceHistory(symbolCode string, limit int) ([]models.AIResponseResult, error) {
	if limit < 1 || limit > 100 {
		limit = 12
	}
	var records []models.AIResponseResult
	err := db.Dao.Where("stock_code = ? AND question = ?", normalizeSymbolKey(symbolCode), "Stock King 结构化研究建议").
		Order("created_at DESC").Limit(limit).Find(&records).Error
	return records, err
}

func (a *App) addConfiguredAIExplanation(advice map[string]any) {
	settings := data.GetSettingConfig()
	var selected *data.AIConfig
	for _, candidate := range settings.AiConfigs {
		if candidate != nil && strings.TrimSpace(candidate.ApiKey) != "" && strings.TrimSpace(candidate.BaseUrl) != "" && strings.TrimSpace(candidate.ModelName) != "" {
			selected = candidate
			break
		}
	}
	if selected == nil {
		advice["llmExplanationStatus"] = "not_configured"
		advice["llmExplanationMessage"] = "未配置 AI 平台；已保留可审计的结构化建议。"
		return
	}

	payload, err := json.Marshal(advice)
	if err != nil {
		advice["llmExplanationStatus"] = "unavailable"
		advice["llmExplanationMessage"] = err.Error()
		return
	}
	// Keep the explanation prompt bounded. The underlying evidence and numeric
	// forecast remain in the response even when the explanatory model fails.
	runes := []rune(string(payload))
	if len(runes) > 30000 {
		runes = runes[:30000]
	}
	messages := []map[string]interface{}{
		{
			"role":    "system",
			"content": "你是 Stock King 证据解释器。只解释输入证据；不得修改 SafeBound、BalancedRank、LimitPulse 的原始信号、已学习权重、概率、区间、门槛、候选排名或用户笔记，不得补造横截面百分位或未来 K 线。按三档分别说明结果与计算过程，再说明多空依据、风险、观察条件和证伪条件，并明确这不是保证收益的投资建议。",
		},
		{
			"role":    "user",
			"content": "请解释以下结构化证据：\n" + string(runes),
		},
	}
	openAI := data.NewDeepSeekOpenAi(a.ctx, int(selected.ID))
	chunks := make(chan map[string]any, 256)
	go func() {
		defer close(chunks)
		data.AskAi(openAI, nil, messages, chunks, "Stock King 结构化建议解释", false)
	}()
	var explanation strings.Builder
	failed := false
	for chunk := range chunks {
		if code, ok := chunk["code"].(int); ok && code == 0 {
			failed = true
		}
		if content, ok := chunk["content"].(string); ok {
			explanation.WriteString(content)
		}
	}
	if failed || strings.TrimSpace(explanation.String()) == "" {
		advice["llmExplanationStatus"] = "unavailable"
		advice["llmExplanationMessage"] = strings.TrimSpace(explanation.String())
		return
	}
	advice["llmExplanationStatus"] = "completed"
	advice["llmExplanation"] = explanation.String()
	advice["llmExplanationModel"] = selected.ModelName
}

func (a *App) StartQuantTraining(horizons []int, trainMaster bool) (map[string]any, error) {
	symbols, metadata, err := eligibleQuantUniverse(true)
	if err != nil {
		return nil, err
	}
	if len(symbols) == 0 {
		return nil, fmt.Errorf("股票基础数据尚未就绪，请稍后重试；量化训练至少需要一只合格 A 股")
	}
	return a.dailyMap(http.MethodPost, "/api/v1/quant/training/tasks", map[string]any{
		"symbols": symbols, "horizons": horizons,
		"objectives":        []string{"conservative", "regular", "aggressive"},
		"universe_metadata": metadata, "train_master": trainMaster,
		"legacy_forecasts": false,
	})
}

// eligibleQuantSymbols reads the actual GORM table used by StockBasic. The old
// bridge queried a non-existent `stock_basic` table, silently produced an empty
// universe, and every training request was rejected by the sidecar.
func eligibleQuantSymbols() ([]string, error) {
	symbols, _, err := eligibleQuantUniverse(false)
	return symbols, err
}

func eligibleQuantUniverse(includeDelisted bool) ([]string, map[string]map[string]any, error) {
	var stocks []data.StockBasic
	query := db.Dao.Model(&data.StockBasic{}).
		Select("ts_code, name, industry, bk_name, market, exchange, list_status, list_date, delist_date")
	if err := query.Find(&stocks).Error; err != nil {
		return nil, nil, fmt.Errorf("读取量化训练股票池失败: %w", err)
	}

	// tushare_stock_basic is also incrementally refreshed from TDX. That feed
	// contains funds, bonds, and indices in addition to equities, and legacy
	// databases may contain duplicate rows because ts_code was not unique.
	// Sending the raw table used to exceed the sidecar's 7,000-symbol request
	// guard and made both training buttons fail with HTTP 422 before any work
	// started. Select one metadata-rich row per genuine A-share first.
	stockBySymbol := make(map[string]data.StockBasic, len(stocks))
	for _, stock := range stocks {
		symbol := normalizeSymbolKey(stock.TsCode)
		if !isQuantAShareSymbol(symbol) {
			continue
		}
		current, exists := stockBySymbol[symbol]
		if !exists || quantStockMetadataScore(stock) > quantStockMetadataScore(current) {
			stockBySymbol[symbol] = stock
		}
	}

	symbols := make([]string, 0, len(stockBySymbol))
	metadata := make(map[string]map[string]any, len(stockBySymbol))
	cutoff := time.Now().AddDate(0, -6, 0).Format("20060102")
	for symbol, stock := range stockBySymbol {
		status := strings.ToUpper(strings.TrimSpace(stock.ListStatus))
		if includeDelisted {
			// Historical panels retain delisted securities to avoid survivorship bias.
			if status != "" && status != "L" && status != "D" {
				continue
			}
		} else {
			name := strings.ToUpper(strings.TrimSpace(stock.Name))
			if (status != "" && status != "L") || strings.Contains(name, "ST") || strings.Contains(name, "退") {
				continue
			}
		}
		if stock.ListDate != "" && stock.ListDate > cutoff {
			continue
		}
		symbols = append(symbols, symbol)
		metadata[symbol] = map[string]any{
			"name": stock.Name, "industry": stock.Industry, "bk_name": stock.BKName,
			"market": stock.Market, "exchange": stock.Exchange,
			"list_status": stock.ListStatus,
			"list_date":   stock.ListDate, "delist_date": stock.DelistDate,
		}
	}
	sort.Strings(symbols)
	return symbols, metadata, nil
}

func quantStockMetadataScore(stock data.StockBasic) int {
	score := 0
	for _, value := range []string{
		stock.Name, stock.Industry, stock.BKName, stock.Market, stock.Exchange,
		stock.ListStatus, stock.ListDate, stock.DelistDate,
	} {
		if strings.TrimSpace(value) != "" {
			score++
		}
	}
	return score
}

func isQuantAShareSymbol(symbol string) bool {
	parts := strings.Split(strings.ToUpper(strings.TrimSpace(symbol)), ".")
	if len(parts) != 2 || !isSixDigitCode(parts[0]) {
		return false
	}
	code, exchange := parts[0], parts[1]
	switch exchange {
	case "SH":
		prefix := code[:3]
		return (prefix >= "600" && prefix <= "605") || prefix == "688" || prefix == "689"
	case "SZ":
		switch code[:3] {
		case "000", "001", "002", "003", "300", "301":
			return true
		}
	case "BJ":
		return code[0] == '4' || code[0] == '8' || strings.HasPrefix(code, "92")
	}
	return false
}

// StartQuantBacktest runs the same leakage-safe rolling evaluation without a
// monthly MASTER retrain. Each candidate still has to pass the release gates.
func (a *App) StartQuantBacktest(horizons []int) (map[string]any, error) {
	return a.StartQuantTraining(horizons, false)
}

func (a *App) GetQuantTask(taskID string) (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/quant/tasks/"+url.PathEscape(strings.TrimSpace(taskID)), nil)
}

func (a *App) PauseQuantTask(taskID string) (map[string]any, error) {
	return a.dailyMap(http.MethodPost, "/api/v1/quant/tasks/"+url.PathEscape(strings.TrimSpace(taskID))+"/pause", map[string]any{})
}

func (a *App) ResumeQuantTask(taskID string) (map[string]any, error) {
	return a.dailyMap(http.MethodPost, "/api/v1/quant/tasks/"+url.PathEscape(strings.TrimSpace(taskID))+"/resume", map[string]any{})
}

func (a *App) ListQuantTasks(limit int) (map[string]any, error) {
	if limit < 1 || limit > 100 {
		limit = 20
	}
	return a.dailyMap(http.MethodGet, fmt.Sprintf("/api/v1/quant/tasks?limit=%d", limit), nil)
}

func (a *App) GetModelMetrics() (map[string]any, error) {
	return a.dailyMap(http.MethodGet, "/api/v1/quant/metrics", nil)
}

func (a *App) dailyMap(method, path string, input any) (map[string]any, error) {
	if a.sidecar == nil {
		return nil, fmt.Errorf("Daily engine manager is unavailable")
	}
	ctx := a.ctx
	if ctx == nil {
		ctx = context.Background()
	}
	var output map[string]any
	if err := a.sidecar.Request(ctx, method, path, input, &output); err != nil {
		return nil, err
	}
	return output, nil
}

func normalizeSymbolKey(value string) string {
	normalized := strings.ToUpper(strings.TrimSpace(value))
	if len(normalized) == 8 {
		exchange := normalized[:2]
		code := normalized[2:]
		if (exchange == "SH" || exchange == "SZ" || exchange == "BJ") && isSixDigitCode(code) {
			return code + "." + exchange
		}
	}
	if isSixDigitCode(normalized) {
		exchange := "SZ"
		if strings.HasPrefix(normalized, "6") || strings.HasPrefix(normalized, "9") {
			exchange = "SH"
		} else if strings.HasPrefix(normalized, "4") || strings.HasPrefix(normalized, "8") {
			exchange = "BJ"
		}
		return normalized + "." + exchange
	}
	return normalized
}

func isSixDigitCode(value string) bool {
	if len(value) != 6 {
		return false
	}
	for _, char := range value {
		if char < '0' || char > '9' {
			return false
		}
	}
	return true
}

func (a *App) buildGoTechnicalSnapshot(symbolCode string) map[string]any {
	symbol := normalizeSymbolKey(symbolCode)
	result := a.GetStockKLineWithFallback(symbol, "", "101", 220, "qfq")
	if result == nil || result.Data == nil || len(*result.Data) < 30 {
		return map[string]any{"available": false, "source": "go_stock_kline"}
	}
	rows := append([]data.KLineData(nil), (*result.Data)...)
	sort.Slice(rows, func(i, j int) bool { return rows[i].Day < rows[j].Day })
	type technicalBar struct {
		day             string
		open, high, low float64
		close, volume   float64
	}
	bars := make([]technicalBar, 0, len(rows))
	for _, row := range rows {
		closeValue, closeErr := strconv.ParseFloat(strings.TrimSpace(row.Close), 64)
		highValue, highErr := strconv.ParseFloat(strings.TrimSpace(row.High), 64)
		lowValue, lowErr := strconv.ParseFloat(strings.TrimSpace(row.Low), 64)
		openValue, openErr := strconv.ParseFloat(strings.TrimSpace(row.Open), 64)
		volumeValue, _ := strconv.ParseFloat(strings.TrimSpace(row.Volume), 64)
		if closeErr == nil && highErr == nil && lowErr == nil && openErr == nil && closeValue > 0 && highValue > 0 && lowValue > 0 {
			bars = append(bars, technicalBar{day: row.Day, open: openValue, high: highValue, low: lowValue, close: closeValue, volume: volumeValue})
		}
	}
	if len(bars) < 30 {
		return map[string]any{"available": false, "source": result.Source}
	}
	closes := make([]float64, len(bars))
	volumes := make([]float64, len(bars))
	for index, bar := range bars {
		closes[index] = bar.close
		volumes[index] = bar.volume
	}
	mean := func(values []float64, window int) float64 {
		if len(values) < window || window <= 0 {
			return 0
		}
		total := 0.0
		for _, value := range values[len(values)-window:] {
			total += value
		}
		return total / float64(window)
	}
	stddev := func(values []float64, window int) float64 {
		average := mean(values, window)
		if average == 0 || len(values) < window {
			return 0
		}
		variance := 0.0
		for _, value := range values[len(values)-window:] {
			delta := value - average
			variance += delta * delta
		}
		return math.Sqrt(variance / float64(window))
	}
	emaSeries := func(values []float64, period int) []float64 {
		if len(values) == 0 {
			return nil
		}
		alpha := 2.0 / float64(period+1)
		output := make([]float64, len(values))
		output[0] = values[0]
		for index := 1; index < len(values); index++ {
			output[index] = alpha*values[index] + (1-alpha)*output[index-1]
		}
		return output
	}
	returnAt := func(window int) float64 {
		if len(closes) <= window || closes[len(closes)-window-1] <= 0 {
			return 0
		}
		return closes[len(closes)-1]/closes[len(closes)-window-1] - 1
	}
	latest := closes[len(closes)-1]
	returns := make([]float64, 0, 20)
	start := len(closes) - 20
	if start < 1 {
		start = 1
	}
	for index := start; index < len(closes); index++ {
		returns = append(returns, closes[index]/closes[index-1]-1)
	}
	avgReturn := 0.0
	for _, value := range returns {
		avgReturn += value
	}
	if len(returns) > 0 {
		avgReturn /= float64(len(returns))
	}
	variance := 0.0
	for _, value := range returns {
		delta := value - avgReturn
		variance += delta * delta
	}
	volatility := 0.0
	if len(returns) > 0 {
		volatility = math.Sqrt(variance / float64(len(returns)))
	}

	ma5 := mean(closes, 5)
	ma10 := mean(closes, 10)
	ma20 := mean(closes, 20)
	ma60 := mean(closes, 60)
	trend := "均线交织"
	if ma60 > 0 && latest > ma5 && ma5 > ma10 && ma10 > ma20 && ma20 > ma60 {
		trend = "多头排列"
	} else if ma60 > 0 && latest < ma5 && ma5 < ma10 && ma10 < ma20 && ma20 < ma60 {
		trend = "空头排列"
	}

	gain, loss := 0.0, 0.0
	for index := len(closes) - 14; index < len(closes); index++ {
		if index <= 0 {
			continue
		}
		delta := closes[index] - closes[index-1]
		if delta >= 0 {
			gain += delta
		} else {
			loss -= delta
		}
	}
	rsi14 := 50.0
	if loss == 0 && gain > 0 {
		rsi14 = 100
	} else if loss > 0 {
		rsi14 = 100 - 100/(1+gain/loss)
	}

	ema12 := emaSeries(closes, 12)
	ema26 := emaSeries(closes, 26)
	difSeries := make([]float64, len(closes))
	for index := range closes {
		difSeries[index] = ema12[index] - ema26[index]
	}
	deaSeries := emaSeries(difSeries, 9)
	macdDif := difSeries[len(difSeries)-1]
	macdDea := deaSeries[len(deaSeries)-1]
	macdHistogram := 2 * (macdDif - macdDea)

	bollMiddle := ma20
	bollDeviation := stddev(closes, 20)
	bollUpper := bollMiddle + 2*bollDeviation
	bollLower := bollMiddle - 2*bollDeviation
	bollPosition := 0.5
	if bollUpper > bollLower {
		bollPosition = (latest - bollLower) / (bollUpper - bollLower)
	}

	trueRanges := make([]float64, 0, 14)
	for index := len(bars) - 14; index < len(bars); index++ {
		if index <= 0 {
			continue
		}
		bar := bars[index]
		previousClose := bars[index-1].close
		trueRange := math.Max(bar.high-bar.low, math.Max(math.Abs(bar.high-previousClose), math.Abs(bar.low-previousClose)))
		trueRanges = append(trueRanges, trueRange)
	}
	atr14 := mean(trueRanges, len(trueRanges))
	atrPercent := 0.0
	if latest > 0 {
		atrPercent = atr14 / latest
	}

	kValue, dValue := 50.0, 50.0
	for index := range bars {
		windowStart := index - 8
		if windowStart < 0 {
			windowStart = 0
		}
		lowest, highest := bars[windowStart].low, bars[windowStart].high
		for _, bar := range bars[windowStart : index+1] {
			lowest = math.Min(lowest, bar.low)
			highest = math.Max(highest, bar.high)
		}
		rsv := 50.0
		if highest > lowest {
			rsv = (bars[index].close - lowest) / (highest - lowest) * 100
		}
		kValue = (2*kValue + rsv) / 3
		dValue = (2*dValue + kValue) / 3
	}
	jValue := 3*kValue - 2*dValue

	recentBars := bars[len(bars)-20:]
	support, resistance := recentBars[0].low, recentBars[0].high
	for _, bar := range recentBars[1:] {
		support = math.Min(support, bar.low)
		resistance = math.Max(resistance, bar.high)
	}
	pricePosition20 := 0.5
	if resistance > support {
		pricePosition20 = (latest - support) / (resistance - support)
	}
	volumeMA5 := mean(volumes, 5)
	volumeMA20 := mean(volumes, 20)
	volumeRatio5To20 := 0.0
	if volumeMA20 > 0 {
		volumeRatio5To20 = volumeMA5 / volumeMA20
	}

	return map[string]any{
		"available": true, "source": result.Source, "close": latest,
		"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
		"aboveMa20": latest > ma20, "trend": trend,
		"return5": returnAt(5), "return20": returnAt(20), "return60": returnAt(60),
		"volatility20": volatility, "rsi14": rsi14,
		"macdDif": macdDif, "macdDea": macdDea, "macdHistogram": macdHistogram,
		"macdBullish": macdDif > macdDea,
		"kdjK":        kValue, "kdjD": dValue, "kdjJ": jValue,
		"bollUpper": bollUpper, "bollMiddle": bollMiddle, "bollLower": bollLower, "bollPosition": bollPosition,
		"atr14": atr14, "atrPercent": atrPercent,
		"volumeMa5": volumeMA5, "volumeMa20": volumeMA20, "volumeRatio5To20": volumeRatio5To20,
		"support20": support, "resistance20": resistance, "pricePosition20": pricePosition20,
		"dataDate": bars[len(bars)-1].day,
	}
}

func (a *App) initStockKingSchedule() {
	if a.cron == nil || a.sidecar == nil {
		return
	}
	jobs := []struct{ slot, expression string }{
		{"0920", "0 10 9 * * 1-5"}, {"0940", "0 30 9 * * 1-5"},
		{"0955", "0 45 9 * * 1-5"}, {"1030", "0 20 10 * * 1-5"},
		{"1455", "0 45 14 * * 1-5"}, {"review", "0 30 15 * * 1-5"}, {"weekly", "0 45 15 * * 5"},
	}
	for _, job := range jobs {
		slot := job.slot
		run := func() {
			if (slot == "weekly" && !backgroundLearningEnabled(a.paths)) || (slot != "weekly" && slot != "review" && !autoRecommendationsEnabled(a.paths)) {
				return
			}
			_, err := a.dailyMap(http.MethodPost, "/api/v1/stock-king/picks/runs", map[string]any{"scan_slot": slot, "top_n": 5})
			if err != nil {
				logger.SugaredLogger.Warnf("Stock King local scheduled job %s failed: %v", slot, err)
			}
		}
		if id, err := a.cron.AddFunc("CRON_TZ=Asia/Shanghai "+job.expression, run); err == nil {
			a.setCronEntry("stockKingV11-"+slot, id)
		} else {
			logger.SugaredLogger.Warnf("Stock King schedule %s: %v", slot, err)
		}
	}
}
func validateJSON(value string) bool {
	return value == "" || json.Valid([]byte(value))
}
