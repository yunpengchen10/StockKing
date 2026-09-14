package main

import (
	"go-stock/backend/db"
	"math"
	"path/filepath"
	"testing"
)

func sampleInvestmentPlan() InvestmentPlan {
	return InvestmentPlan{ID: "test-plan-1", AccountID: 1, Symbol: "600000.SH", Horizon: "short", EntryPrice: 10, StopPrice: 9, Quantity: 100, ExpiresOn: "2026-09-10", ReviewOn: "2026-09-12", Status: "watching"}
}
func TestInvestmentPlanRejectsInvalidRiskInputs(t *testing.T) {
	plan := sampleInvestmentPlan()
	if err := validateInvestmentPlan(plan); err != nil {
		t.Fatal(err)
	}
	plan.StopPrice = 11
	if validateInvestmentPlan(plan) == nil {
		t.Fatal("stop above entry accepted")
	}
	plan = sampleInvestmentPlan()
	plan.EntryPrice = math.NaN()
	if validateInvestmentPlan(plan) == nil {
		t.Fatal("NaN accepted")
	}
	plan = sampleInvestmentPlan()
	plan.Quantity = 50
	if validateInvestmentPlan(plan) == nil {
		t.Fatal("new plan accepted below trading unit")
	}
	plan.Status = "holding"
	if err := validateInvestmentPlan(plan); err != nil {
		t.Fatalf("actual remaining odd-lot holding must be attributable: %v", err)
	}
}
func TestInvestmentPlanKeepsOriginalHorizonAndHistory(t *testing.T) {
	db.Init(filepath.Join(t.TempDir(), "plans.db"))
	conn, err := db.Dao.DB()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	if err := db.Dao.AutoMigrate(&StockKingPreference{}); err != nil {
		t.Fatal(err)
	}
	app := &App{}
	saved, err := app.SaveInvestmentPlan(sampleInvestmentPlan())
	if err != nil {
		t.Fatal(err)
	}
	saved.Horizon = "long"
	if _, err = app.SaveInvestmentPlan(saved); err == nil {
		t.Fatal("short failure silently became long holding")
	}
	saved.Horizon = "short"
	saved.Status = "cancelled"
	if _, err = app.SaveInvestmentPlan(saved); err != nil {
		t.Fatal(err)
	}
	rows, err := app.ListInvestmentPlans()
	if err != nil || len(rows) != 1 || rows[0].Status != "cancelled" || rows[0].CreatedAt == "" {
		t.Fatalf("history not retained: %v %v", rows, err)
	}
}
