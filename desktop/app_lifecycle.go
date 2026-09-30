package main

import (
	"context"
	"fmt"
	"github.com/duke-git/lancet/v2/convertor"
	"github.com/duke-git/lancet/v2/strutil"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/runtime"
	"go-stock/backend/data"
	"go-stock/backend/db"
	"go-stock/backend/logger"
	"time"
)

// A second shortcut launch restores the existing window, including a window
// hidden by an older updater. Wails also focuses/restores minimized windows.
func (a *App) onSecondInstanceLaunch(_ options.SecondInstanceData) {
	if a.ctx != nil {
		runtime.WindowShow(a.ctx)
	}
}

// startup is called at application startup
func (a *App) startup(ctx context.Context) {
	defer PanicHandler()

	data.ConfigureFromSettings(data.GetSettingConfig())

	runtime.EventsOn(ctx, "frontendError", func(optionalData ...interface{}) {
		logger.SugaredLogger.Errorf("Frontend error: %v\n", optionalData)
	})

	a.ctx = ctx
	if a.sidecar != nil {
		a.sidecar.Start(ctx)
	}
	a.initStockKingSchedule()

	// 设置全局 Wails 上下文，供 AI 工具修改分组/概念后向前端推送刷新事件
	data.SetAppCtx(ctx)

	a.InitCronTasks()

	preCacheTradingDays()

	runtime.EventsOn(ctx, "updateSettings", func(optionalData ...interface{}) {
		config := data.GetSettingConfig()
		if config.DarkTheme {
			runtime.WindowSetBackgroundColour(ctx, 27, 38, 54, 1)
			runtime.WindowSetDarkTheme(ctx)
		} else {
			runtime.WindowSetBackgroundColour(ctx, 255, 255, 255, 1)
			runtime.WindowSetLightTheme(ctx)
		}
		runtime.WindowReloadApp(ctx)
	})
}

func MonitorStockPrices(a *App) {
	// 检查是否至少有一个市场开市
	isAStockOpen := isTradingTime(time.Now())
	isHKStockOpen := IsHKTradingTime(time.Now())
	isUSStockOpen := IsUSTradingTime(time.Now())

	// 如果所有市场都不在交易时间，则提前返回
	if !isAStockOpen && !isHKStockOpen && !isUSStockOpen {
		//logger.SugaredLogger.Debugf("当前所有市场均未开市，跳过价格监控")
		return
	}

	//logger.SugaredLogger.Debugf("市场状态 - A股: %v, 港股: %v, 美股: %v", isAStockOpen, isHKStockOpen, isUSStockOpen)

	dest := &[]data.FollowedStock{}
	db.Dao.Model(&data.FollowedStock{}).Find(dest)
	total := float64(0)
	//for _, follow := range *dest {
	//	stockData := getStockInfo(follow)
	//	total += stockData.ProfitAmountToday
	//	price, _ := convertor.ToFloat(stockData.Price)
	//	if stockData.PrePrice != price {
	//		go runtime.EventsEmit(a.ctx, "stock_price", stockData)
	//	}
	//}

	stockInfos := GetStockInfos(*dest...)
	for _, stockInfo := range *stockInfos {
		if strutil.HasPrefixAny(stockInfo.Code, []string{"SZ", "SH", "sh", "sz"}) && (!isTradingTime(time.Now())) {
			continue
		}
		if strutil.HasPrefixAny(stockInfo.Code, []string{"hk", "HK"}) && (!IsHKTradingTime(time.Now())) {
			continue
		}
		if strutil.HasPrefixAny(stockInfo.Code, []string{"us", "US", "gb_"}) && (!IsUSTradingTime(time.Now())) {
			continue
		}

		total += stockInfo.ProfitAmountToday
		price, _ := convertor.ToFloat(stockInfo.Price)

		if stockInfo.PrePrice != price {
			//logger.SugaredLogger.Infof("-----------sz------------股票代码: %s, 股票名称: %s, 股票价格: %s,盘前盘后:%s", stockInfo.Code, stockInfo.Name, stockInfo.Price, stockInfo.BA)
			go runtime.EventsEmit(a.ctx, "stock_price", stockInfo)
		}

	}
	go runtime.EventsEmit(a.ctx, "realtime_profit", fmt.Sprintf("  %.2f", total))

}

// beforeClose is called when the application is about to quit,
// either by clicking the window close button or calling runtime.Quit.
// Returning true will cause the application to continue, false will continue shutdown as normal.
func (a *App) beforeClose(ctx context.Context) (prevent bool) {
	defer PanicHandler()

	if a.ctx != nil {
		w, h := runtime.WindowGetSize(ctx)
		if w > 0 && h > 0 {
			cfg := data.GetSettingConfig()
			cfg.WindowWidth = w
			cfg.WindowHeight = h
			data.UpdateConfig(cfg)
		}

	}
	a.stopBackgroundServices()
	return false
}
