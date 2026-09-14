package data

import (
	"encoding/base64"
	"os"
	"strings"
	"testing"
)

func TestRenderTechStyleImage(t *testing.T) {
	md := `# 贵州茅台 (600519)

## 核心财务指标

| 指标 | 2024Q3 | 变化 |
|------|--------|------|
| 营收 | 1231亿 | +16.5% |
| 净利润 | 608亿 | +15.2% |
| 毛利率 | 91.2% | +0.3% |
| ROE | 28.5% | -1.2% |

### 机构预测

- 目标价: **2180元**
- 评级: 买入
- 一致预期EPS: ` + "`68.5`" + `

> 数据来源：东方财富妙想 AI 分析

---

**风险提示**: 以上内容仅供参考`

	result, err := markdownToImage(md)
	if err != nil {
		t.Fatalf("markdownToImage failed: %v", err)
	}

	if len(result) < 100 {
		t.Fatalf("result too short, got %d chars", len(result))
	}

	if !strings.HasPrefix(result, "base64://") {
		t.Fatalf("result should start with base64://, got %s", result[:min(20, len(result))])
	}

	if os.Getenv("SAVE_TEST_IMAGE") == "1" {
		decoded, err := base64.StdEncoding.DecodeString(result[len("base64://"):])
		if err != nil {
			t.Fatalf("base64 decode failed: %v", err)
		}
		err = os.WriteFile("test_tech_style_dark.png", decoded, 0644)
		if err != nil {
			t.Fatalf("write file failed: %v", err)
		}
		t.Logf("图片已保存到 test_tech_style_dark.png, 大小: %d bytes", len(decoded))
	}
}

func TestRenderLightThemeImage(t *testing.T) {
	cfg := GetSettingConfig()
	original := cfg.DarkTheme
	cfg.DarkTheme = false
	defer func() { cfg.DarkTheme = original }()

	md := `## 市场热点

| 板块 | 涨幅 | 资金流入 |
|------|------|----------|
| AI芯片 | +5.2% | +38亿 |
| 机器人 | +3.8% | +22亿 |

- **核心标的**: 寒武纪、海光信息
- 驱动因素: 政策催化 + 业绩超预期`

	result, err := markdownToImage(md)
	if err != nil {
		t.Fatalf("markdownToImage failed: %v", err)
	}

	if len(result) < 100 {
		t.Fatalf("result too short, got %d chars", len(result))
	}

	if os.Getenv("SAVE_TEST_IMAGE") == "1" {
		decoded, err := base64.StdEncoding.DecodeString(result[len("base64://"):])
		if err != nil {
			t.Fatalf("base64 decode failed: %v", err)
		}
		err = os.WriteFile("test_tech_style_light.png", decoded, 0644)
		if err != nil {
			t.Fatalf("write file failed: %v", err)
		}
		t.Logf("图片已保存到 test_tech_style_light.png, 大小: %d bytes", len(decoded))
	}
}
