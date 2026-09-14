package data

import (
	"go-stock/backend/db"
	"go-stock/backend/models"
	"strings"
	"time"

	"github.com/duke-git/lancet/v2/datetime"
	"github.com/duke-git/lancet/v2/strutil"
)

type AIResponseResultService struct{}

func NewAIResponseResultService() *AIResponseResultService {
	return &AIResponseResultService{}
}

// GetAIResponseResultList 分页查询AI响应结果
func (s *AIResponseResultService) GetAIResponseResultList(query models.AIResponseResultQuery) (*models.AIResponseResultPageData, error) {
	var list []models.AIResponseResult
	var total int64

	q := db.Dao.Model(&models.AIResponseResult{})

	// 同一个搜索词会同时传入多个字段，因此用括号内 OR 组合；旧实现未
	// 接住 GORM 返回值，导致这些筛选条件实际没有生效。
	var clauses []string
	var args []any
	for _, field := range []struct{ column, value string }{
		{column: "chat_id", value: query.ChatId},
		{column: "model_name", value: query.ModelName},
		{column: "stock_code", value: query.StockCode},
		{column: "stock_name", value: query.StockName},
		{column: "question", value: query.Question},
	} {
		if strings.TrimSpace(field.value) != "" {
			clauses = append(clauses, field.column+" LIKE ?")
			args = append(args, "%"+strings.TrimSpace(field.value)+"%")
		}
	}
	if len(clauses) > 0 {
		q = q.Where("("+strings.Join(clauses, " OR ")+")", args...)
	}
	if query.StartDate != "" && query.EndDate != "" {
		query.StartDate = strutil.ReplaceWithMap(query.StartDate, map[string]string{
			"T": " ",
			"Z": "",
		})
		query.EndDate = strutil.ReplaceWithMap(query.EndDate, map[string]string{
			"T": " ",
			"Z": "",
		})

		startDate, err := time.Parse("2006-01-02 15:04:05", query.StartDate)
		if err != nil {
			startDate, _ = time.Parse("2006-01-02", query.StartDate)
		}

		endDate, err := time.Parse("2006-01-02 15:04:05", query.EndDate)
		if err != nil {
			endDate, _ = time.Parse("2006-01-02", query.EndDate)
		}
		q = q.Where("created_at BETWEEN ? AND ?", datetime.BeginOfDay(startDate), datetime.EndOfDay(endDate))
		//q = q.Where("created_at BETWEEN ? AND ?", query.StartDate, query.EndDate)
	}

	// 计算总数
	err := q.Count(&total).Error
	if err != nil {
		return nil, err
	}

	// 设置默认分页参数
	page := query.Page
	pageSize := query.PageSize
	if page <= 0 {
		page = 1
	}
	if pageSize <= 0 || pageSize > 100 {
		pageSize = 10
	}

	// 执行分页查询
	offset := (page - 1) * pageSize
	err = q.Offset(offset).Limit(pageSize).Order("created_at DESC").Find(&list).Error
	if err != nil {
		return nil, err
	}

	totalPages := int((total + int64(pageSize) - 1) / int64(pageSize))

	return &models.AIResponseResultPageData{
		List:       list,
		Total:      total,
		Page:       page,
		PageSize:   pageSize,
		TotalPages: totalPages,
	}, nil
}

// DeleteAIResponseResult 根据ID删除AI响应结果
func (s *AIResponseResultService) DeleteAIResponseResult(id uint) error {

	// 使用软删除
	result := db.Dao.Where("id = ?", id).Delete(&models.AIResponseResult{})

	return result.Error
}

// BatchDeleteAIResponseResult 批量删除AI响应结果
func (s *AIResponseResultService) BatchDeleteAIResponseResult(ids []uint) error {
	// 使用软删除
	result := db.Dao.Where("id IN ?", ids).Delete(&models.AIResponseResult{})

	return result.Error
}
