import js from '@eslint/js'
import globals from 'globals'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

/**
 * 早期版本零工具链（.local/recon-frontend.md 问题 P-17）：`frontend/` 下无
 * `eslint*` / `prettier*` / `.editorconfig`，`package.json` 无 lint 脚本，
 * CI 前端 job 无 lint 步骤。后果是全仓普遍存在数百字符的单行声明。
 *
 * 注意：格式化由 Prettier 负责（`npm run lint` 里 `prettier --check`），
 * 本文件只做代码质量规则，避免两套工具互相打架。
 */
export default [
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**', 'playwright-report/**', 'test-results/**'] },

  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],

  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
        ecmaVersion: 'latest',
        sourceType: 'module',
      },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      '@typescript-eslint/consistent-type-imports': ['error', { prefer: 'type-imports' }],

      'vue/multi-word-component-names': 'off',

      // 格式类规则一律交给 Prettier（npm run lint 里的 `prettier --check`）。
      // 让两个工具都管换行/缩进只会互相打架，并制造大量无意义告警——
      // 早期版本正是"没有格式化工具"导致全仓超长单行，而"
      // 有工具但两套规则冲突"是同样坏的另一种极端。
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
      'vue/html-indent': 'off',
      'vue/html-closing-bracket-newline': 'off',
      'vue/attributes-order': 'off',
      'vue/block-order': ['error', { order: ['script', 'template', 'style'] }],

      'no-console': ['error', { allow: ['warn', 'error'] }],
      eqeqeq: ['error', 'always'],
      'prefer-const': 'error',
    },
  },

  {
    files: ['tests/**/*.ts', '*.config.{js,ts}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      'no-console': 'off',
    },
  },

  {
    // 仓库自带的验证脚本（`.verify-*.mjs`）：文件本身是 Node 脚本，
    // 但 `page.evaluate()` 里的回调运行在浏览器上下文，两套 globals 都要给。
    files: ['**/*.mjs'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
]
