// Package marketminute reads dated MAC bars without settings, accounts or a database.
package marketminute

import (
	"context"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/bensema/gotdx/proto"
	"github.com/bensema/gotdx/types"
)

// Collect advances by the actual row count. The public MAC service commonly
// returns 699 rows for a 700-row request; that is not the end of its history.
func Collect(page func(uint32, uint16) ([]proto.MACSymbolBar, error), count int, end string) ([]proto.MACSymbolBar, error) {
	if count <= 0 || count > 32000 {
		return nil, fmt.Errorf("bar count must be between 1 and 32000")
	}
	end = strings.TrimSpace(end)
	if len(end) == 8 {
		end += "235959"
	}
	if end != "" {
		if _, err := time.Parse("20060102150405", end); err != nil {
			return nil, fmt.Errorf("invalid historical minute cutoff")
		}
	}
	seen := map[string]proto.MACSymbolBar{}
	selected := map[string]proto.MACSymbolBar{}
	var offset uint32
	for requests := 0; requests < 80 && len(selected) < count; requests++ {
		pageSize := count - len(selected) + 1 // Ask two when one remains; count=1 may return no row.
		if pageSize > 700 {
			pageSize = 700
		}
		rows, err := page(offset, uint16(pageSize))
		if err != nil {
			return nil, err
		}
		if len(rows) == 0 {
			break
		}
		newRows := 0
		for _, row := range rows {
			stamp := row.DateTime.Format("20060102150405")
			previous, exists := seen[stamp]
			if exists && (previous.Open != row.Open || previous.High != row.High || previous.Low != row.Low || previous.Close != row.Close) {
				return nil, fmt.Errorf("conflicting MAC prices across pages at %s", stamp)
			}
			if !exists {
				newRows++
				seen[stamp] = row
				if end == "" || stamp <= end {
					selected[stamp] = row
				}
			}
		}
		if newRows == 0 {
			break // A provider ignoring offset must not loop or manufacture history.
		}
		offset += uint32(len(rows))
	}
	keys := make([]string, 0, len(selected))
	for stamp := range selected {
		keys = append(keys, stamp)
	}
	sort.Strings(keys)
	if len(keys) > count {
		keys = keys[len(keys)-count:]
	}
	result := make([]proto.MACSymbolBar, 0, len(keys))
	for _, stamp := range keys {
		row := selected[stamp]
		if row.FloatShares > 0 && row.Vol > 0 {
			row.Turnover = math.Round(row.Vol/(row.FloatShares*10000)*100*100) / 100
		}
		result = append(result, row)
	}
	return result, nil
}

// BarClient permits bounded connection pools while retaining the same provider
// identity, cursor and completed-minute checks for every transport.
type BarClient interface {
	ConnectMAC() error
	GetMACSymbolBars(uint8, string, uint16, uint16, uint32, uint16, uint16) (*proto.MACSymbolBarsReply, error)
}

func Fetch(client BarClient, market uint8, code string, period uint16, count int, adjust uint16, end string) ([]proto.MACSymbolBar, error) {
	return FetchContext(context.Background(), client, market, code, period, count, adjust, end)
}

func FetchContext(ctx context.Context, client BarClient, market uint8, code string, period uint16, count int, adjust uint16, end string) ([]proto.MACSymbolBar, error) {
	// Freeze the cutoff before connecting or reading page 1. A partial minute
	// from page 1 cannot become completed while subsequent pages are downloaded.
	if period == types.KLINE_TYPE_1MIN || period == types.KLINE_TYPE_EXHQ_1MIN {
		end = minuteCutoff(end, time.Now())
	}
	if err := client.ConnectMAC(); err != nil {
		return nil, err
	}
	return Collect(func(start uint32, size uint16) ([]proto.MACSymbolBar, error) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		reply, err := client.GetMACSymbolBars(market, code, period, 1, start, size, adjust)
		if err != nil {
			return nil, err
		}
		if err := ValidateReply(reply, market, code, period, start); err != nil {
			return nil, err
		}
		return reply.List, nil
	}, count, end)
}

// ValidateReply checks the provider's echoed identity before any prices can be
// attributed to a requested symbol. A nonempty, well-formed OHLC page is not
// evidence that the provider answered the requested security or cursor.
func ValidateReply(reply *proto.MACSymbolBarsReply, market uint8, code string, period uint16, start uint32) error {
	if reply == nil {
		return fmt.Errorf("missing MAC reply")
	}
	normalize := func(value string) string { return strings.ToUpper(strings.Trim(value, " \x00\t\r\n")) }
	if reply.Market != uint16(market) || normalize(reply.Code) != normalize(code) || uint16(reply.Period) != period || reply.Start != start {
		return fmt.Errorf("MAC response identity mismatch: requested market=%d code=%s period=%d start=%d; received market=%d code=%s period=%d start=%d", market, code, period, start, reply.Market, normalize(reply.Code), reply.Period, reply.Start)
	}
	return nil
}

func minuteCutoff(end string, started time.Time) string {
	end = strings.TrimSpace(end)
	if len(end) == 8 {
		end += "235959"
	}
	cutoff := started.Format("20060102150405")
	if end == "" || len(end) == 14 && end > cutoff {
		return cutoff
	}
	return end
}
