package main

import (
	"context"
	"encoding/json"
	"go-stock/backend/data"
	"go-stock/backend/db"
	"go-stock/backend/logger"
	"go-stock/backend/models"
	"go-stock/backend/util"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func initTestDB(t *testing.T) {
	t.Helper()
	if db.Dao != nil {
		if sqlDB, err := db.Dao.DB(); err == nil {
			_ = sqlDB.Close()
		}
	}
	db.Init(filepath.Join(t.TempDir(), "stock-king-test.db"))
	current := db.Dao
	t.Cleanup(func() {
		if db.Dao == current {
			if sqlDB, err := current.DB(); err == nil {
				_ = sqlDB.Close()
			}
		}
	})
}

// @Author spark
// @Date 2025/2/24 9:35
// @Desc
// -----------------------------------------------------------------------------------
func TestIsHKTradingTime(t *testing.T) {
	f := IsHKTradingTime(time.Now())
	t.Log(f)
}

func TestIsUSTradingTime(t *testing.T) {

	date := time.Now()
	hour, minute, _ := date.Clock()
	logger.SugaredLogger.Infof("当前时间: %d:%d", hour, minute)

	t.Log(IsUSTradingTime(time.Now()))
}

func TestCheckStockBaseInfo(t *testing.T) {
	t.Skip("external stock metadata integration test")
	db.Init("./data/stock.db")
	NewApp(AppPaths{}).CheckStockBaseInfo(context.Background())
}

func TestJson(t *testing.T) {
	initTestDB(t)

	jsonStr := "{\n\t\t\"id\" : 3334,\n\t\t\"created_at\" : \"2025-02-28 16:49:31.8342514+08:00\",\n\t\t\"updated_at\" : \"2025-02-28 16:49:31.8342514+08:00\",\n\t\t\"deleted_at\" : null,\n\t\t\"code\" : \"PUK.US\",\n\t\t\"name\" : \"英国保诚集团\",\n\t\t\"full_name\" : \"\",\n\t\t\"e_name\" : \"\",\n\t\t\"exchange\" : \"NASDAQ\",\n\t\t\"type\" : \"stock\",\n\t\t\"is_del\" : 0,\n\t\t\"bk_name\" : null,\n\t\t\"bk_code\" : null\n\t}"

	v := &models.StockInfoUS{}
	json.Unmarshal([]byte(jsonStr), v)
	logger.SugaredLogger.Infof("v:%+v", v)

	db.Dao.Model(v).Updates(v)

}

func TestGetScreenResolution(t *testing.T) {
	x, y, w, h, err := getScreenResolution()
	if err != nil {
		logger.SugaredLogger.Errorf("get screen resolution error:%s", err.Error())
		return
	}
	logger.SugaredLogger.Infof("x:%d,y:%d,w:%d,h:%d", x, y, w, h)

}

func TestLegacySponsorAndUpdateAPIsAreRemoved(t *testing.T) {
	appType := reflect.TypeOf(&App{})
	for _, name := range []string{"CheckUpdate", "CheckSponsorCode", "GetSponsorInfo", "GetEffectiveSponsorVip", "QuitApp"} {
		if _, exists := appType.MethodByName(name); exists {
			t.Fatalf("legacy API %s must not be exposed", name)
		}
	}
}

func TestGetAiRecommendStocksList(t *testing.T) {
	initTestDB(t)
	if err := db.Dao.AutoMigrate(&models.AiRecommendStocks{}); err != nil {
		t.Fatal(err)
	}

	str := "{\"startDate\": \"2026-03-20 00:00:00\", \"endDate\": \"2026-03-27 23:59:59\", \"page\": 1, \"pageSize\": 5000}"
	query := &models.AiRecommendStocksQuery{}
	json.Unmarshal([]byte(str), query)

	pageData, err := data.NewAiRecommendStocksService().GetAiRecommendStocksList(query)
	if err != nil {
		t.Fatal(err)
	}
	logger.SugaredLogger.Infof("pageData:%+v", pageData.List)
	var dataExport []models.AiRecommendStocksMdExport
	for _, v := range pageData.List {
		dataExport = append(dataExport, v.ToMdExportStruct())
	}
	content := util.MarkdownTableWithTitle("近期AI分析/推荐股票明细列表", dataExport)
	logger.SugaredLogger.Infof("content:%s", content)
}

func TestSummaryStockNews(t *testing.T) {
	t.Skip("requires a configured external LLM")
	db.Init("./data/stock.db")
	question := "分析今日的市场行情走势是否和券商的观点一致"
	app := NewApp(AppPaths{})
	msgs := data.NewDeepSeekOpenAi(app.ctx, 0).NewSummaryStockNewsStreamWithTools(question, nil, app.AiTools, true, nil)

	content := &strings.Builder{}
	for msg := range msgs {
		logger.SugaredLogger.Infof("msg:%+v", msg)
		content.WriteString(msg["content"].(string))
	}
	logger.SugaredLogger.Infof("content:%s", content.String())
}

func TestCalculateNextRunTime(t *testing.T) {
	initTestDB(t)
	t.Log(NewApp(AppPaths{}).CalculateNextRunTime("0 0 0 * * ?"))
}

func TestFetchAiModels(t *testing.T) {
	t.Skip("requires an external AI provider")
	app := NewApp(AppPaths{})
	models := app.FetchAiModels("https://ark.cn-beijing.volces.com/api/v3", "")
	t.Log(models)

}

func TestFetchAiModelsOpenAICompatible(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/models" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer sense-token" {
			t.Fatalf("unexpected authorization header: %q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[{"id":"SenseChat-5"},{"id":"SenseNova-V6.5-Pro"}]}`))
	}))
	defer server.Close()

	models := NewApp(AppPaths{}).FetchAiModels(server.URL, "sense-token")
	if len(models) != 2 || models[0] != "SenseChat-5" || models[1] != "SenseNova-V6.5-Pro" {
		t.Fatalf("unexpected compatible model list: %#v", models)
	}
}

func TestSenseNovaPresetIsOfficialCompatibleEndpoint(t *testing.T) {
	source, err := os.ReadFile(filepath.Join("frontend", "src", "components", "ai-config-manager.vue"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(source)
	for _, required := range []string{
		"https://api.sensenova.cn/compatible-mode/v2",
		"SenseChat-5",
		"SenseNova-V6.5-Pro",
		"SenseNova-V6.5-Turbo",
	} {
		if !strings.Contains(text, required) {
			t.Fatalf("SenseNova preset is missing %q", required)
		}
	}
}

func TestChinaMarketColorSemantics(t *testing.T) {
	constants, err := os.ReadFile(filepath.Join("frontend", "src", "components", "kline", "constants.ts"))
	if err != nil {
		t.Fatal(err)
	}
	chart, err := os.ReadFile(filepath.Join("frontend", "src", "components", "StockLightweightKlineChart.vue"))
	if err != nil {
		t.Fatal(err)
	}
	// Market directions are independent of the interface's blue action color.
	if !strings.Contains(string(constants), "CLR_RISE = '#f05264'") || !strings.Contains(string(constants), "CLR_FALL = '#22ab94'") {
		t.Fatal("candlestick constants must retain A-share red-up / green-down semantics")
	}
	if !strings.Contains(string(chart), "upColor: CLR_RISE") || !strings.Contains(string(chart), "downColor: CLR_FALL") {
		t.Fatal("desktop candlesticks must use the same A-share direction colors")
	}
}

func TestGetLatestTradingDay(t *testing.T) {
	app := NewApp(AppPaths{})
	date := app.GetLatestTradingDay()
	t.Log(date)
}
