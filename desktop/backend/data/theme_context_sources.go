package data

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"go-stock/backend/db"
	"go-stock/backend/models"
	"sort"
	"time"

	"gorm.io/gorm"
)

var sharedThemeEvents = &themeEventCache{}

// FetchSharedConceptEvents is the HTTP-only source shared by market UI and
// unattended research. It never calls crawler/HotStock/browser APIs.
func FetchSharedConceptEvents(ctx context.Context, date string) ([]models.ConceptEventDay, time.Time, error) {
	ctx, cancel := context.WithTimeout(ctx, 6*time.Second)
	defer cancel()
	return sharedThemeEvents.load(ctx, date, time.Now, func(ctx context.Context, date string) ([]models.ConceptEventDay, error) {
		response, err := SharedHTTPClient.R().SetContext(ctx).
			SetHeader("Origin", "https://news.10jqka.com.cn").
			SetHeader("Referer", "https://news.10jqka.com.cn/").
			SetHeader("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0").
			SetQueryParam("date", date).
			Get("https://news.10jqka.com.cn/app/concept_v2_api/open/api/concept/event/jtcsm/v1/event/list")
		if err != nil {
			return nil, err
		}
		if response.StatusCode() != 200 || len(response.Body()) > 4*1024*1024 {
			return nil, fmt.Errorf("concept events HTTP status %d or oversized body", response.StatusCode())
		}
		var payload struct {
			StatusCode *int                     `json:"status_code"`
			Data       []models.ConceptEventDay `json:"data"`
		}
		if err := json.Unmarshal(response.Body(), &payload); err != nil {
			return nil, err
		}
		if payload.StatusCode == nil || *payload.StatusCode != 0 || payload.Data == nil {
			return nil, errors.New("concept events provider reported failure or missing data")
		}
		return payload.Data, nil
	})
}

func themeFundsFromDB(ctx context.Context, dao *gorm.DB, now time.Time, limit int) ThemeContextSection {
	result := themeContextSection("desktop:concept-fund-flow-db/eastmoney", "provider quote time unavailable; snapshotObservedAt is local collection time", now)
	result.Gaps = append(result.Gaps, "资金数据库未保留供应商报价源时；仅供背景研究，不能证明同窗实时资金方向")
	if dao == nil {
		result.Gaps = append(result.Gaps, "市场数据库未就绪")
		return result
	}
	day := now.In(themeContextTZ).Format("2006-01-02")
	// Existing installations index (code,snap_time), not snap_time alone. A
	// whole-table DISTINCT/MAX scan can exceed the deadline on years of data.
	// Read a bounded primary-key tail and disclose that scope, without writing
	// an index or presenting this local capture tail as complete market history.
	const tailLimit = 5000
	var tail []models.ConceptFundFlow
	err := dao.WithContext(ctx).Select("id, code, name, net_inflow, snap_time, created_at").Order("id DESC").Limit(tailLimit).Find(&tail).Error
	if err != nil {
		result.Gaps = append(result.Gaps, "概念资金历史读取失败")
		return result
	}
	groups := map[string][]models.ConceptFundFlow{}
	for _, row := range tail {
		at, parseErr := time.ParseInLocation("2006-01-02 15:04:05", row.SnapTime, themeContextTZ)
		if parseErr != nil || at.Format("2006-01-02") != day || at.After(now) || (!row.CreatedAt.IsZero() && row.CreatedAt.After(now)) {
			continue
		}
		groups[row.SnapTime] = append(groups[row.SnapTime], row)
	}
	snapshots := make([]string, 0, len(groups))
	for stamp := range groups {
		snapshots = append(snapshots, stamp)
	}
	sort.Sort(sort.Reverse(sort.StringSlice(snapshots)))
	result.Coverage = map[string]any{"scope": "bounded_latest_capture_tail", "tailLimit": tailLimit,
		"receivedCount": len(tail), "historyComplete": len(tail) < tailLimit}
	if len(tail) == tailLimit {
		result.Gaps = append(result.Gaps, "仅复用数据库最近5000条采集记录，未声称完整历史资金覆盖")
	}
	if len(snapshots) == 0 {
		result.Status = "empty"
		result.Gaps = append(result.Gaps, "今日尚无概念资金采集记录")
		return result
	}
	rows := groups[snapshots[0]]
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].NetInflow != rows[j].NetInflow {
			return rows[i].NetInflow > rows[j].NetInflow
		}
		return rows[i].Code < rows[j].Code
	})
	result.Coverage["latestCaptureObservedCount"] = len(rows)
	if len(rows) > limit {
		rows = rows[:limit]
	}
	previous := map[string]models.ConceptFundFlow{}
	if len(snapshots) > 1 {
		for _, row := range groups[snapshots[1]] {
			previous[row.Code] = row
		}
	}
	for _, row := range rows {
		observed, err := time.ParseInLocation("2006-01-02 15:04:05", row.SnapTime, themeContextTZ)
		if err != nil || observed.After(now) || (!row.CreatedAt.IsZero() && row.CreatedAt.After(now)) {
			continue
		}
		item := map[string]any{"code": row.Code, "name": row.Name, "netInflow": row.NetInflow,
			"snapshotObservedAt": themeContextISO(observed), "sourceTime": nil, "quoteTimeVerified": false}
		if before, ok := previous[row.Code]; ok {
			at, parseErr := time.ParseInLocation("2006-01-02 15:04:05", before.SnapTime, themeContextTZ)
			if parseErr == nil && at.Before(observed) && (before.CreatedAt.IsZero() || !before.CreatedAt.After(now)) {
				item["previousNetInflow"] = before.NetInflow
				item["previousSnapshotObservedAt"] = themeContextISO(at)
				item["observedNetInflowChange"] = row.NetInflow - before.NetInflow
			}
		}
		result.Items = append(result.Items, item)
	}
	if len(result.Items) > 0 {
		result.Status, result.LatestSourceDate = "observed", day
	} else {
		result.Status = "empty"
	}
	return result
}

func themeNewsFromDB(ctx context.Context, dao *gorm.DB, now time.Time, limit int) ThemeContextSection {
	result := themeContextSection("desktop:market-news-db", "news publication time; not quote time", now)
	if dao == nil {
		result.Gaps = append(result.Gaps, "市场数据库未就绪")
		return result
	}
	var rows []models.Telegraph
	err := dao.WithContext(ctx).Where("data_time >= ? AND data_time <= ? AND created_at <= ?", now.Add(-72*time.Hour), now, now).
		Order("data_time DESC, id DESC").Limit(limit).Find(&rows).Error
	if err != nil {
		result.Gaps = append(result.Gaps, "已保存市场资讯读取失败")
		return result
	}
	today := now.In(themeContextTZ).Format("2006-01-02")
	current := false
	for _, row := range rows {
		if row.DataTime == nil || row.DataTime.IsZero() || row.DataTime.After(now) || row.Source == "" {
			continue
		}
		stamp := themeContextISO(*row.DataTime)
		date := row.DataTime.In(themeContextTZ).Format("2006-01-02")
		current = current || date == today
		if result.SourceTime == nil || stamp > *result.SourceTime {
			result.SourceTime = &stamp
		}
		if date > result.LatestSourceDate {
			result.LatestSourceDate = date
		}
		var observedAt *string
		if !row.CreatedAt.IsZero() {
			observed := themeContextISO(row.CreatedAt)
			observedAt = &observed
		}
		result.Items = append(result.Items, map[string]any{"id": row.ID, "source": row.Source,
			"title": row.Title, "content": row.Content, "url": row.Url, "sourceTime": stamp,
			"observedAt": observedAt, "currentDay": date == today, "stockAssociation": "not_inferred_from_news_text"})
	}
	if len(result.Items) == 0 {
		result.Status = "empty"
		result.Gaps = append(result.Gaps, "近72小时无可核对发布时间的市场资讯")
	} else if current {
		result.Status = "observed"
	} else {
		result.Status = "reference_only"
	}
	return result
}

func FetchSharedThemeContext(ctx context.Context, date string, limit int) ThemeMarketContext {
	now := time.Now()
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	if limit <= 0 || limit > 200 {
		limit = 100
	}
	result := ThemeMarketContext{Version: ThemeContextVersion, ObservedAt: themeContextISO(now),
		Events: themeContextSection("ths:concept-events", "event publication time; not quote time", now),
		Funds:  themeContextSection("desktop:concept-fund-flow-db/eastmoney", "provider quote time unavailable", now),
		News:   themeContextSection("desktop:market-news-db", "news publication time; not quote time", now)}
	validated, err := ValidateThemeContextDate(date, now)
	if err != nil {
		result.Events.Gaps = append(result.Events.Gaps, err.Error())
		return result
	}
	type completed struct {
		name  string
		value ThemeContextSection
	}
	ready := make(chan completed, 3)
	go func() {
		days, received, err := FetchSharedConceptEvents(ctx, validated)
		value := result.Events
		if err == nil {
			value = buildThemeEvents(days, received, time.Now(), limit)
		} else {
			value.Gaps = append(value.Gaps, "同花顺结构化题材读取失败或超时")
		}
		ready <- completed{"events", value}
	}()
	go func() {
		dbCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		ready <- completed{"funds", themeFundsFromDB(dbCtx, db.Dao, now, limit)}
	}()
	go func() {
		dbCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		ready <- completed{"news", themeNewsFromDB(dbCtx, db.Dao, now, limit)}
	}()
	for i := 0; i < 3; i++ {
		select {
		case <-ctx.Done():
			return result
		case item := <-ready:
			switch item.name {
			case "events":
				result.Events = item.value
			case "funds":
				result.Funds = item.value
			case "news":
				result.News = item.value
			}
		}
	}
	result.ObservedAt = themeContextISO(time.Now())
	return result
}
