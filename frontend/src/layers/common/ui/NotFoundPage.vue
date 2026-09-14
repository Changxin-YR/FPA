<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <main class="not-found" role="alert" data-testid="not-found">
    <h1>页面不存在</h1>
    <p>该地址没有对应的页面。可能是链接过时、或该资源尚未在服务端登记。</p>
    <RouterLink to="/workbench" data-testid="not-found-home">回到工作台</RouterLink>
  </main>
</template>

<style scoped>
.not-found {
  padding: 24px;
}
.not-found h1 {
  margin-bottom: 8px;
}
</style>
