package data

import (
	"context"
	"errors"
	"fmt"
	"go-stock/backend/db"
	"go-stock/backend/models"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/glebarez/sqlite"
	"github.com/go-resty/resty/v2"
	"gorm.io/gorm"
)

func themeContextTestNow() time.Time {
	return time.Date(2026, 11, 13, 10, 30, 0, 0, themeContextTZ)
}

func TestThemeContextPreservesPriorDatesPublicationTimesAndEventAssociation(t *testing.T) {
	now := themeContextTestNow()
	published := now.Add(-24 * time.Hour)
	days := []models.ConceptEventDay{{Date: "2026-11-12", EventList: []models.ConceptEvent{{
		EventID: "real-event", Title: "算力产业进展", Heat: 200, CreateTime: published.UnixMilli(),
		Themes:    []models.ConceptTheme{{ID: "one", Type: "concept", ShowName: "算力"}, {ID: "two", Type: "industry", ShowName: "通信"}},
		TopStocks: []models.ConceptTopStock{{StockCode: "600000", StockName: "示例股票"}},
	}}}}
	result := buildThemeEvents(days, now, now, 100)
	if result.Status != "reference_only" || result.LatestSourceDate != "2026-11-12" || len(result.Items) != 1 {
		t.Fatalf("prior data was lost or made current: %+v", result)
	}
	if result.SourceTime == nil || *result.SourceTime != themeContextISO(published) || result.ObservedAt != themeContextISO(now) {
		t.Fatal("publication time became request time")
	}
	row := result.Items[0]
	if row["currentDay"] != false || row["stockAssociation"] != "event_scope_not_verified_theme_membership" || len(row["topStocks"].([]models.ConceptTopStock)) != 1 {
		t.Fatal("event stocks were misrepresented as verified members")
	}
}

func TestThemeContextExcludesFutureEventsAndDoesNotInventMissingTime(t *testing.T) {
	now := themeContextTestNow()
	days := []models.ConceptEventDay{
		{Date: "2026-11-13", EventList: []models.ConceptEvent{
			{EventID: "future", Title: "未来事件", CreateTime: now.Add(time.Minute).Unix()},
			{EventID: "missing", Title: "发布时间缺失", CreateTime: 0},
		}},
		{Date: "2026-11-14", EventList: []models.ConceptEvent{{EventID: "tomorrow", Title: "未来日期"}}},
		{Date: "2026-10-01", EventList: []models.ConceptEvent{{EventID: "old", Title: "过期"}}},
	}
	result := buildThemeEvents(days, now, now, 100)
	if result.Status != "observed" || len(result.Items) != 1 || result.Items[0]["eventId"] != "missing" || result.SourceTime != nil {
		t.Fatalf("bad event dates accepted: %+v", result)
	}
	if got := buildThemeEvents(nil, now, now, 100); got.Status != "empty" {
		t.Fatal("empty source declared observed")
	}
}

func TestThemeContextDateRejectsHistoricalCurrentFetch(t *testing.T) {
	now := themeContextTestNow()
	for _, date := range []string{"2026-11-12", "2026-11-14", "../../source", "2026-11-13T10:30:00"} {
		if _, err := ValidateThemeContextDate(date, now); err == nil {
			t.Fatalf("bad request date accepted: %s", date)
		}
	}
	if date, err := ValidateThemeContextDate("", now); err != nil || date != "2026-11-13" {
		t.Fatal(date, err)
	}
}

func TestThemeContextAllASnapshotIncludesConceptPeersWithoutChangingDefault(t *testing.T) {
	main := snapshotUniverseFilter(false)
	all := snapshotUniverseFilter(true)
	if main != `(MARKET in ("上交所主板","深交所主板"))` {
		t.Fatal("default universe changed", main)
	}
	for _, board := range []string{"上交所主板", "深交所主板", "深交所创业板", "上交所科创板", "上交所风险警示板", "深交所风险警示板"} {
		if !strings.Contains(all, `"`+board+`"`) {
			t.Fatalf("missing %s peers", board)
		}
	}
	if strings.Contains(all, "北京") || strings.Contains(all, "B股") {
		t.Fatal("non SH/SZ A shares included")
	}
}

func TestThemeContextSnapshotHTTPUsesStableFullScopeAndKeepsLegacyScope(t *testing.T) {
	queries := []url.Values{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		queries = append(queries, r.URL.Query())
		_, _ = w.Write([]byte(`{"success":true,"result":{"data":[],"count":0,"nextpage":false}}`))
	}))
	defer server.Close()
	client := resty.New().SetTransport(&themeContextTestTransport{target: server.URL, delegate: http.DefaultTransport})
	api := StockDataApi{client: client, config: &SettingConfig{Settings: &Settings{CrawlTimeOut: 1}}}
	api.GetAllAStocksForSnapshot(1, 500)
	api.GetAllStocks(1, 500, "", models.TechnicalIndicators{}, true)
	api.GetAllStocks(1, 500, "", models.TechnicalIndicators{})
	if len(queries) != 3 {
		t.Fatal("missing actual provider requests")
	}
	for index, allA := range []bool{true, false} {
		query := queries[index]
		if query.Get("st") != "SECURITY_CODE" || query.Get("sr") != "1" || query.Get("filter") != snapshotUniverseFilter(allA) || strings.Contains(query.Get("sty"), "CONCEPT") {
			t.Fatalf("bad snapshot request: %+v", query)
		}
	}
	if queries[2].Get("st") != "CHANGE_RATE" || !strings.Contains(queries[2].Get("filter"), "北京") {
		t.Fatal("legacy UI selection changed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	api.GetAllAStocksForSnapshotContext(ctx, 1, 500)
	if len(queries) != 3 {
		t.Fatal("cancelled snapshot reached provider")
	}
}

func TestThemeContextSharedCacheCoalescesAndPreservesOriginalObservation(t *testing.T) {
	var cache themeEventCache
	now := themeContextTestNow()
	var calls atomic.Int32
	release := make(chan struct{})
	started := make(chan struct{})
	fetch := func(context.Context, string) ([]models.ConceptEventDay, error) {
		if calls.Add(1) == 1 {
			close(started)
		}
		<-release
		return []models.ConceptEventDay{{Date: "2026-11-13", EventList: []models.ConceptEvent{{Title: "原始事件"}}}}, nil
	}
	var group sync.WaitGroup
	for i := 0; i < 8; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			_, _, _ = cache.load(context.Background(), "2026-11-13", func() time.Time { return now }, fetch)
		}()
	}
	<-started
	close(release)
	group.Wait()
	if calls.Load() != 1 {
		t.Fatalf("duplicate UI/research fetches: %d", calls.Load())
	}
	first, observed, _ := cache.load(context.Background(), "2026-11-13", func() time.Time { return now.Add(10 * time.Second) }, fetch)
	first[0].EventList[0].Title = "changed by caller"
	second, secondAt, _ := cache.load(context.Background(), "2026-11-13", func() time.Time { return now.Add(20 * time.Second) }, fetch)
	if second[0].EventList[0].Title != "原始事件" || !observed.Equal(now) || !secondAt.Equal(now) {
		t.Fatal("cache mutation or receipt time renewal")
	}
}

func TestThemeContextCacheCancellationAndFailureDoNotBecomeSuccess(t *testing.T) {
	var cache themeEventCache
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	called := false
	_, _, err := cache.load(ctx, "2026-11-13", time.Now, func(context.Context, string) ([]models.ConceptEventDay, error) {
		called = true
		return nil, nil
	})
	if called || !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled request reached source")
	}
	_, _, err = cache.load(context.Background(), "2026-11-13", time.Now, func(context.Context, string) ([]models.ConceptEventDay, error) {
		return nil, errors.New("provider unavailable")
	})
	if err == nil || len(cache.Entries) != 0 {
		t.Fatal("source failure cached as successful empty data")
	}
}

func themeContextTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	dao, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	connection, err := dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	connection.SetMaxOpenConns(1)
	t.Cleanup(func() { _ = connection.Close() })
	if err := dao.AutoMigrate(&models.ConceptFundFlow{}, &models.Telegraph{}); err != nil {
		t.Fatal(err)
	}
	return dao
}

func TestThemeContextFundsReuseTodayDBWithoutInventingSourceTime(t *testing.T) {
	dao, now := themeContextTestDB(t), themeContextTestNow()
	rows := []models.ConceptFundFlow{
		{Code: "BK1000", Name: "算力", NetInflow: 100, SnapTime: "2026-11-13 10:28:00", CreatedAt: now.Add(-2 * time.Minute)},
		{Code: "BK1000", Name: "算力", NetInflow: 180, SnapTime: "2026-11-13 10:29:00", CreatedAt: now.Add(-time.Minute)},
		{Code: "BK2000", Name: "未来", NetInflow: 9999, SnapTime: "2026-11-13 10:31:00", CreatedAt: now.Add(time.Minute)},
		{Code: "BK3000", Name: "昨日", NetInflow: 8888, SnapTime: "2026-11-12 15:00:00", CreatedAt: now.Add(-24 * time.Hour)},
	}
	if err := dao.Create(&rows).Error; err != nil {
		t.Fatal(err)
	}
	result := themeFundsFromDB(context.Background(), dao, now, 100)
	if result.Status != "observed" || len(result.Items) != 1 || result.SourceTime != nil {
		t.Fatalf("invalid funds: %+v", result)
	}
	row := result.Items[0]
	if row["observedNetInflowChange"] != int64(80) || row["sourceTime"] != nil || row["quoteTimeVerified"] != false {
		t.Fatalf("capture clock or delta overstated: %+v", row)
	}
}

func TestThemeContextNewsUsesPublishedAndKnownTimes(t *testing.T) {
	dao, now := themeContextTestDB(t), themeContextTestNow()
	published, future := now.Add(-time.Minute), now.Add(time.Minute)
	rows := []models.Telegraph{
		{Model: gorm.Model{CreatedAt: published}, DataTime: &published, Title: "产业进展", Source: "财联社电报"},
		{Model: gorm.Model{CreatedAt: published}, DataTime: &future, Title: "未来发布时间", Source: "财联社电报"},
		{Model: gorm.Model{CreatedAt: published}, DataTime: nil, Time: "10:29:00", Title: "无日期", Source: "新浪财经"},
		{Model: gorm.Model{CreatedAt: future}, DataTime: &published, Title: "未来才入库", Source: "新浪财经"},
	}
	if err := dao.Create(&rows).Error; err != nil {
		t.Fatal(err)
	}
	result := themeNewsFromDB(context.Background(), dao, now, 100)
	if len(result.Items) != 1 || result.Items[0]["title"] != "产业进展" || result.SourceTime == nil || *result.SourceTime != themeContextISO(published) {
		t.Fatalf("invalid news clocks: %+v", result)
	}
	if funds := themeFundsFromDB(context.Background(), nil, now, 100); funds.Status != "unavailable" {
		t.Fatal("missing DB hidden")
	}
}

func TestThemeContextHTTPRejectsFailedPayloadWithoutBrowserFallback(t *testing.T) {
	oldClient, oldCache := SharedHTTPClient, sharedThemeEvents
	t.Cleanup(func() { SharedHTTPClient, sharedThemeEvents = oldClient, oldCache })
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"status_code":1,"data":[]}`))
	}))
	defer server.Close()
	SharedHTTPClient = resty.New().SetTransport(&themeContextTestTransport{target: server.URL, delegate: http.DefaultTransport})
	sharedThemeEvents = &themeEventCache{}
	if _, _, err := FetchSharedConceptEvents(context.Background(), "2026-11-13"); err == nil {
		t.Fatal("failed provider payload became observed")
	}
}

func TestThemeContextIndependentPartsRetainEventsWhenFundsFail(t *testing.T) {
	oldClient, oldCache, oldDB := SharedHTTPClient, sharedThemeEvents, db.Dao
	t.Cleanup(func() { SharedHTTPClient, sharedThemeEvents, db.Dao = oldClient, oldCache, oldDB })
	now := time.Now().In(themeContextTZ)
	published := now.Add(-time.Minute)
	dao := themeContextTestDB(t)
	if err := dao.Migrator().DropTable(&models.ConceptFundFlow{}); err != nil {
		t.Fatal(err)
	}
	news := models.Telegraph{Model: gorm.Model{CreatedAt: published}, DataTime: &published, Title: "已有资讯", Source: "财联社电报"}
	if err := dao.Create(&news).Error; err != nil {
		t.Fatal(err)
	}
	db.Dao = dao
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"status_code":0,"data":[{"date":%q,"eventList":[{"eventId":"one","title":"真实结构事件","createTime":%d}]}]}`, now.Format("2006-01-02"), published.Unix())
	}))
	defer server.Close()
	SharedHTTPClient = resty.New().SetTransport(&themeContextTestTransport{target: server.URL, delegate: http.DefaultTransport})
	sharedThemeEvents = &themeEventCache{}
	result := FetchSharedThemeContext(context.Background(), "", 100)
	if result.Events.Status != "observed" || len(result.Events.Items) != 1 || result.Funds.Status != "unavailable" || result.News.Status != "observed" {
		t.Fatalf("one failed part erased independent evidence: %+v", result)
	}
}

type themeContextTestTransport struct {
	target   string
	delegate http.RoundTripper
}

func (transport *themeContextTestTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if err := request.Context().Err(); err != nil {
		return nil, err
	}
	forward, err := http.NewRequestWithContext(request.Context(), request.Method, transport.target, nil)
	if err != nil {
		return nil, err
	}
	forward.URL.RawQuery = request.URL.RawQuery
	return transport.delegate.RoundTrip(forward)
}
