<script setup lang="ts">
import {h} from 'vue'
import {NTag,NImage} from 'naive-ui'
import EmbeddedUrl from "./EmbeddedUrl.vue";
</script>

<template>
  <n-tabs  type="line" animated>
    <n-tab-pane name="选股通" tab="选股通">
      <embedded-url url="https://xuangutong.com.cn"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>
    <n-tab-pane name="百度股市通" tab="百度股市通">
      <embedded-url url="https://gushitong.baidu.com"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>
    <n-tab-pane name="东财大盘星图" tab="东财大盘星图">
      <embedded-url url="https://quote.eastmoney.com/stockhotmap/"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>
    <n-tab-pane name="TopHub" tab="TopHub(今日热榜)">
      <embedded-url url="https://tophub.today/c/finance"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>
    <n-tab-pane name="财联社-行情数据" tab="财联社-行情数据">
      <embedded-url url="https://www.cls.cn/quotation"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>
    <n-tab-pane name="现货黄金" tab="现货黄金走势">
      <embedded-url url="https://www.tradinghero.com/chart?symbol=XAUUSD.GOODS"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>

    <n-tab-pane name="金十期货" tab="金十期货">
      <embedded-url url="https://qihuo.jin10.com/"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>
    <n-tab-pane name="金十数据" tab="金十数据">
      <embedded-url url="https://datas.jin10.com/#/category/52005"  :height="'calc(100vh - 252px)'"/>
    </n-tab-pane>


    <n-tab-pane name="欢迎推荐更多有趣的财经网页" tab="欢迎推荐更多有趣的财经网页">
    </n-tab-pane>
<!--    <n-tab-pane name="自在量化" tab="自在量化">-->
<!--      <embedded-url url="https://quant.zizizaizai.com/home"/>-->
<!--    </n-tab-pane>-->
  </n-tabs>
</template>

<style scoped>

</style>
