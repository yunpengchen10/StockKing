package data

// These records supply research context. Event-associated stocks are not a
// verified constituent list, and database capture clocks are not quote clocks.
import (
	"context"
	"encoding/json"
	"errors"
	"go-stock/backend/models"
	"sort"
	"sync"
	"time"
)

const ThemeContextVersion = "software-theme-context-v1"

var themeContextTZ = time.FixedZone("Asia/Shanghai", 8*3600)

type ThemeContextSection struct {
	Status            string           `json:"status"`
	Source            string           `json:"source"`
	SourceTime        *string          `json:"sourceTime"`
	ObservedAt        string           `json:"observedAt"`
	SourceTimeMeaning string           `json:"sourceTimeMeaning"`
	LatestSourceDate  string           `json:"latestSourceDate,omitempty"`
	ResearchOnly      bool             `json:"researchOnly"`
	Coverage          map[string]any   `json:"coverage,omitempty"`
	Items             []map[string]any `json:"items"`
	Gaps              []string         `json:"gaps"`
}

type ThemeMarketContext struct {
	Version    string              `json:"version"`
	ObservedAt string              `json:"observedAt"`
	Events     ThemeContextSection `json:"events"`
	Funds      ThemeContextSection `json:"funds"`
	News       ThemeContextSection `json:"news"`
}

func themeContextISO(at time.Time) string { return at.In(themeContextTZ).Format(time.RFC3339Nano) }

func themeContextSection(source, meaning string, at time.Time) ThemeContextSection {
	return ThemeContextSection{Status: "unavailable", Source: source, ObservedAt: themeContextISO(at),
		SourceTimeMeaning: meaning, ResearchOnly: true, Items: []map[string]any{}, Gaps: []string{}}
}

func themeEventSourceTime(epoch int64) *time.Time {
	if epoch <= 0 {
		return nil
	}
	var at time.Time
	if epoch >= 1000000000000 {
		at = time.UnixMilli(epoch)
	} else {
		at = time.Unix(epoch, 0)
	}
	at = at.In(themeContextTZ)
	return &at
}

func buildThemeEvents(days []models.ConceptEventDay, received, now time.Time, limit int) ThemeContextSection {
	result := themeContextSection("ths:concept-events", "latest event publication time; not quote time", received)
	currentDay := now.In(themeContextTZ).Format("2006-01-02")
	seen := map[string]bool{}
	current := false
	for _, day := range days {
		_, err := time.ParseInLocation("2006-01-02", day.Date, themeContextTZ)
		if err != nil || day.Date > currentDay || day.Date < now.In(themeContextTZ).AddDate(0, 0, -7).Format("2006-01-02") {
			continue
		}
		for _, event := range day.EventList {
			identity := day.Date + ":" + event.EventID
			if event.EventID == "" {
				identity += event.Title
			}
			if event.Title == "" || seen[identity] {
				continue
			}
			published := themeEventSourceTime(event.CreateTime)
			if published != nil && published.After(now) {
				continue
			}
			seen[identity] = true
			var sourceTime *string
			if published != nil {
				stamp := themeContextISO(*published)
				sourceTime = &stamp
				if result.SourceTime == nil || stamp > *result.SourceTime {
					result.SourceTime = &stamp
				}
			}
			if day.Date > result.LatestSourceDate {
				result.LatestSourceDate = day.Date
			}
			current = current || day.Date == currentDay
			result.Items = append(result.Items, map[string]any{"date": day.Date, "eventId": event.EventID,
				"title": event.Title, "heat": event.Heat, "sourceTime": sourceTime,
				"observedAt": themeContextISO(received), "currentDay": day.Date == currentDay,
				"investmentDirection": event.InvestmentDirection, "themes": event.Themes,
				"topStocks": event.TopStocks, "stockAssociation": "event_scope_not_verified_theme_membership"})
		}
	}
	sort.SliceStable(result.Items, func(i, j int) bool {
		if result.Items[i]["date"] != result.Items[j]["date"] {
			return result.Items[i]["date"].(string) > result.Items[j]["date"].(string)
		}
		return result.Items[i]["heat"].(int64) > result.Items[j]["heat"].(int64)
	})
	if len(result.Items) > limit {
		result.Items = result.Items[:limit]
	}
	if len(result.Items) == 0 {
		result.Status = "empty"
		result.Gaps = append(result.Gaps, "来源未提供最近七日可用题材事件")
	} else if current {
		result.Status = "observed"
	} else {
		result.Status = "reference_only"
		result.Gaps = append(result.Gaps, "来源尚无今日事件；保留原日期的历史题材线索，需当前行情重新确认")
	}
	if result.SourceTime == nil && len(result.Items) > 0 {
		result.Gaps = append(result.Gaps, "事件发布时间缺失，读取时间没有替代发布时间")
	}
	return result
}

type themeEventCacheEntry struct {
	Days       []models.ConceptEventDay
	ObservedAt time.Time
}

type themeEventFlight struct {
	Done  chan struct{}
	Value themeEventCacheEntry
	Err   error
}

type themeEventCache struct {
	sync.Mutex
	Entries map[string]themeEventCacheEntry
	Flights map[string]*themeEventFlight
}

func cloneThemeEventDays(days []models.ConceptEventDay) []models.ConceptEventDay {
	encoded, _ := json.Marshal(days)
	var copy []models.ConceptEventDay
	_ = json.Unmarshal(encoded, &copy)
	return copy
}

// A caller's deadline bounds waiting, while the underlying source is also given
// its context deadline. Successful UI and research requests share the same cache.
func (cache *themeEventCache) load(ctx context.Context, day string, now func() time.Time,
	fetch func(context.Context, string) ([]models.ConceptEventDay, error)) ([]models.ConceptEventDay, time.Time, error) {
	if err := ctx.Err(); err != nil {
		return nil, time.Time{}, err
	}
	cache.Lock()
	if cache.Entries == nil {
		cache.Entries = map[string]themeEventCacheEntry{}
		cache.Flights = map[string]*themeEventFlight{}
	}
	if value, ok := cache.Entries[day]; ok && !value.ObservedAt.After(now()) && now().Sub(value.ObservedAt) < 60*time.Second {
		cache.Unlock()
		return cloneThemeEventDays(value.Days), value.ObservedAt, nil
	}
	if pending := cache.Flights[day]; pending != nil {
		cache.Unlock()
		select {
		case <-ctx.Done():
			return nil, time.Time{}, ctx.Err()
		case <-pending.Done:
			return cloneThemeEventDays(pending.Value.Days), pending.Value.ObservedAt, pending.Err
		}
	}
	pending := &themeEventFlight{Done: make(chan struct{})}
	cache.Flights[day] = pending
	cache.Unlock()
	days, err := fetch(ctx, day)
	if err == nil && ctx.Err() != nil {
		err = ctx.Err()
	}
	value := themeEventCacheEntry{Days: cloneThemeEventDays(days), ObservedAt: now()}
	cache.Lock()
	if err == nil {
		if len(cache.Entries) > 8 {
			cache.Entries = map[string]themeEventCacheEntry{}
		}
		cache.Entries[day] = value
	}
	pending.Value, pending.Err = value, err
	delete(cache.Flights, day)
	close(pending.Done)
	cache.Unlock()
	return cloneThemeEventDays(value.Days), value.ObservedAt, err
}

// No caller-supplied historical date may turn a current fetch into historical
// point-in-time evidence. Returned source dates remain unchanged.
func ValidateThemeContextDate(date string, now time.Time) (string, error) {
	today := now.In(themeContextTZ).Format("2006-01-02")
	if date == "" {
		return today, nil
	}
	if date != today {
		return "", errors.New("theme context only accepts the current Shanghai date")
	}
	return today, nil
}
