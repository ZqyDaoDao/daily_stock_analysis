'use client'

import { useState, useEffect, useMemo, useCallback } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { watchlistApi, analyzeApi, dataApi } from '@/lib/api'
import { formatNumber, formatPercent, formatDate, formatDateTime, getOperationBadge, getSentimentColor } from '@/lib/utils'
import type { WatchlistItem, AnalysisResult } from '@/lib/types'
import { Loader2, Trash2, Brain, Plus, AlertCircle, ChevronRight, TrendingUp, TrendingDown, Minus, RefreshCw, Download } from 'lucide-react'

export default function WatchlistPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const analyzeParam = searchParams.get('analyze')
  const selectedCodeParam = searchParams.get('code')

  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])
  const [loading, setLoading] = useState(false)
  const [addCode, setAddCode] = useState('')
  const [addName, setAddName] = useState('')
  const [selectedCode, setSelectedCode] = useState<string>('')
  const [selectedAnalysis, setSelectedAnalysis] = useState<AnalysisResult | null>(null)
  const [analyzingCode, setAnalyzingCode] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string>('')

  // 持久化存储所有分析结果
  const [analysisResults, setAnalysisResults] = useState<Record<string, AnalysisResult>>({})

  // 同步状态
  const [syncing, setSyncing] = useState(false)
  const [syncProgress, setSyncProgress] = useState<{ message: string; progress: number } | null>(null)

  // 检查股票是否有数据
  const hasData = useCallback((item: WatchlistItem) => {
    return item.close_price !== null && item.close_price !== undefined
  }, [])

  // 计算有数据和无数据的股票
  const withData = useMemo(() => watchlist.filter(hasData), [watchlist, hasData])
  const withoutData = useMemo(() => watchlist.filter(item => !hasData(item)), [watchlist, hasData])

  // 分析结果映射（使用持久化存储）
  const analysisMap = useMemo(() => {
    return analysisResults
  }, [analysisResults])

  const loadWatchlist = useCallback(async () => {
    setLoading(true)
    try {
      const res = await watchlistApi.list()
      if (res.success && res.data) {
        setWatchlist(res.data)
      }
    } catch (err: any) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  const loadAnalysis = useCallback(async (code: string) => {
    if (analyzingCode.has(code)) return

    setAnalyzingCode(prev => new Set(prev).add(code))

    try {
      const res = await analyzeApi.single(code, true)
      if (res.success && res.data) {
        // 保存到持久化存储
        setAnalysisResults(prev => {
          const next = { ...prev }
          next[code] = res.data!
          return next
        })
        setSelectedAnalysis(res.data)
      }
    } catch (err: any) {
      setError(err.message)
    } finally {
      setAnalyzingCode(prev => {
        const next = new Set(prev)
        next.delete(code)
        return next
      })
    }
  }, [analyzingCode])

  const handleAdd = useCallback(async () => {
    if (!addCode) return

    try {
      // 如果用户没有输入名称，自动获取
      let nameToAdd = addName || undefined
      if (!nameToAdd) {
        try {
          // 尝试从股票详情获取名称
          const stockRes = await fetch(`/api/stocks/${addCode}`)
          if (stockRes.ok) {
            const stockData = await stockRes.json()
            if (stockData.success && stockData.data?.name) {
              nameToAdd = stockData.data.name
            }
          }
        } catch {
          // 如果获取失败，使用默认名称
          nameToAdd = `股票${addCode}`
        }
      }

      const res = await watchlistApi.add({ code: addCode, name: nameToAdd })
      if (res.success) {
        setAddCode('')
        setAddName('')
        loadWatchlist()
      }
    } catch (err: any) {
      setError(err.message)
    }
  }, [addCode, addName, loadWatchlist])

  const handleRemove = useCallback(async (code: string) => {
    try {
      await watchlistApi.remove(code)
      if (selectedCode === code) {
        setSelectedCode('')
        setSelectedAnalysis(null)
      }
      // 清理该股票的分析结果
      setAnalysisResults(prev => {
        const next = { ...prev }
        delete next[code]
        return next
      })
      loadWatchlist()
    } catch (err: any) {
      setError(err.message)
    }
  }, [selectedCode, loadWatchlist])

  const handleSelectStock = useCallback((code: string) => {
    setSelectedCode(code)
    // 从持久化存储中获取分析结果
    if (analysisResults[code]) {
      setSelectedAnalysis(analysisResults[code])
    } else {
      setSelectedAnalysis(null)
    }
  }, [analysisResults])

  const handleAnalyze = useCallback(async (code: string) => {
    await loadAnalysis(code)
  }, [loadAnalysis])

  // 批量选择模式下的处理
  const [batchMode, setBatchMode] = useState(false)
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(new Set())

  // 批量同步选中的股票
  const handleBatchSync = useCallback(async () => {
    if (selectedCodes.size === 0) return

    setSyncing(true)
    setSyncProgress({ message: '正在启动同步任务...', progress: 0 })

    try {
      const codes = Array.from(selectedCodes)
      await dataApi.sync({ codes })

      // 开始轮询同步进度
      const pollInterval = setInterval(async () => {
        try {
          const progressRes = await dataApi.progress()
          if (progressRes.success && progressRes.data) {
            setSyncProgress({
              message: progressRes.data.message || '',
              progress: progressRes.data.progress || 0
            })

            // 同步完成
            if (progressRes.data.progress === 100 || !progressRes.data.is_syncing) {
              clearInterval(pollInterval)
              setSyncing(false)
              setSyncProgress(null)
              setSelectedCodes(new Set())
              setBatchMode(false)
              // 重新加载关注列表
              loadWatchlist()
            }
          }
        } catch (err) {
          console.error('获取同步进度失败:', err)
        }
      }, 1000)

    } catch (err: any) {
      setError(err.message)
      setSyncing(false)
      setSyncProgress(null)
    }
  }, [selectedCodes, loadWatchlist])

  // 批量移除选中的股票
  const handleBatchRemove = useCallback(async () => {
    if (selectedCodes.size === 0) return

    if (!confirm(`确定要从关注列表中移除这 ${selectedCodes.size} 只股票吗？`)) {
      return
    }

    try {
      // 并发删除所有选中的股票
      await Promise.all(
        Array.from(selectedCodes).map(code => watchlistApi.remove(code))
      )

      // 清理选中股票的分析结果
      setAnalysisResults(prev => {
        const next = { ...prev }
        selectedCodes.forEach(code => delete next[code])
        return next
      })

      if (selectedCodes.has(selectedCode)) {
        setSelectedCode('')
        setSelectedAnalysis(null)
      }

      setSelectedCodes(new Set())
      setBatchMode(false)
      loadWatchlist()
    } catch (err: any) {
      setError(err.message)
    }
  }, [selectedCodes, selectedCode, loadWatchlist])

  // 处理URL参数
  useEffect(() => {
    if (selectedCodeParam) {
      setSelectedCode(selectedCodeParam)
      if (analysisMap[selectedCodeParam]) {
        setSelectedAnalysis(analysisMap[selectedCodeParam])
      } else {
        // 如果URL中有分析参数，自动开始分析
        if (analyzeParam) {
          handleAnalyze(selectedCodeParam)
          router.replace('/watchlist')
        }
      }
    }
  }, [selectedCodeParam])

  // 初始化加载
  useEffect(() => {
    loadWatchlist()
  }, [loadWatchlist])

  // 处理批量分析参数
  useEffect(() => {
    if (analyzeParam) {
      const codes = analyzeParam.split(',').filter(c => c)
      if (codes.length > 0) {
        setSelectedCodes(new Set(codes))
        // 分析第一个有数据的股票
        const firstWithCode = codes.find(c => watchlist.find(w => w.code === c && hasData(w)))
        if (firstWithCode) {
          handleAnalyze(firstWithCode)
        }
        router.replace('/watchlist')
      }
    }
  }, [analyzeParam, watchlist, hasData])

  const getPctChgColor = (pct: number | undefined) => {
    if (pct === undefined || pct === null) return 'text-muted-foreground'
    if (pct > 0) return 'text-red-500'
    if (pct < 0) return 'text-green-500'
    return 'text-muted-foreground'
  }

  const getTrendType = (trend: string) => {
    if (trend.includes('买入') || trend.includes('看多')) return 'up'
    if (trend.includes('卖出') || trend.includes('看空')) return 'down'
    return 'neutral'
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">关注列表</h1>
          <p className="text-muted-foreground text-sm">{withData.length} 只有数据 · {withoutData.length} 待同步</p>
        </div>
        <div className="flex gap-2">
          {batchMode ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setBatchMode(false)}>
                <Minus className="h-4 w-4 mr-2" />
                取消
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleBatchRemove}
                disabled={selectedCodes.size === 0 || syncing}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                移除选中 ({selectedCodes.size})
              </Button>
              <Button
                size="sm"
                onClick={handleBatchSync}
                disabled={selectedCodes.size === 0 || syncing}
              >
                {syncing ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    同步中...
                  </>
                ) : (
                  <>
                    <RefreshCw className="h-4 w-4 mr-2" />
                    同步选中 ({selectedCodes.size})
                  </>
                )}
              </Button>
            </>
          ) : (
            <Button size="sm" variant="outline" onClick={() => setBatchMode(true)}>
              批量操作
            </Button>
          )}
        </div>
      </div>

      {/* 同步进度提示 */}
      {syncing && syncProgress && (
        <Card className="border-blue-200 bg-blue-50/50">
          <CardContent className="pt-6">
            <div className="flex items-center gap-3">
              <Loader2 className="h-5 w-5 text-blue-600 animate-spin" />
              <div className="flex-1">
                <div className="font-medium text-blue-800">正在同步数据...</div>
                <div className="text-sm text-blue-700 mt-1">{syncProgress.message}</div>
                <div className="w-full bg-blue-200 rounded-full h-2 mt-2">
                  <div
                    className="bg-blue-600 h-2 rounded-full transition-all"
                    style={{ width: `${syncProgress.progress}%` }}
                  />
                </div>
              </div>
              <div className="text-lg font-bold text-blue-600">{syncProgress.progress}%</div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 添加股票 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">添加股票</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex gap-4">
            <div className="flex-1">
              <Label htmlFor="add-code">股票代码</Label>
              <Input
                id="add-code"
                placeholder="如: 600519"
                value={addCode}
                onChange={(e) => setAddCode(e.target.value)}
              />
            </div>
            <div className="flex-1">
              <Label htmlFor="add-name">股票名称（可选）</Label>
              <Input
                id="add-name"
                placeholder="如: 贵州茅台"
                value={addName}
                onChange={(e) => setAddName(e.target.value)}
              />
            </div>
            <div className="flex items-end">
              <Button onClick={handleAdd} disabled={!addCode} size="sm">
                <Plus className="mr-2 h-4 w-4" />
                添加
              </Button>
            </div>
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            添加后请到"数据管理"页面同步该股票的数据
          </p>
        </CardContent>
      </Card>

      {/* 无数据股票提示 */}
      {withoutData.length > 0 && (
        <Card className="border-yellow-200 bg-yellow-50/50">
          <CardContent className="pt-6">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-yellow-600 mt-0.5" />
              <div>
                <div className="font-medium text-yellow-800">以下股票暂无数据</div>
                <div className="text-sm text-yellow-700 mt-1">
                  {withoutData.map(item => item.code).join(', ')}
                </div>
                <div className="text-sm text-yellow-700 mt-2">
                  请前往"数据管理"页面，使用"下载历史数据"功能输入这些股票代码进行数据同步
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 主内容区：左侧股票列表，右侧分析详情 */}
      <div className="grid gap-4 lg:grid-cols-3">
        {/* 左侧股票列表 */}
        <Card className="lg:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-base">股票列表 ({withData.length})</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {loading ? (
              <div className="flex justify-center py-8">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
              </div>
            ) : watchlist.length === 0 ? (
              <div className="text-center py-8 text-sm text-muted-foreground px-4">
                暂无关注股票
              </div>
            ) : (
              <div className="divide-y">
                {watchlist.map((item) => {
                  const itemHasData = hasData(item)
                  const analysis = analysisMap[item.code]
                  const badge = analysis ? getOperationBadge(analysis.operation_advice) : null
                  const isSelected = batchMode ? selectedCodes.has(item.code) : selectedCode === item.code

                  return (
                    <div
                      key={item.code}
                      className={`p-4 cursor-pointer transition-colors hover:bg-muted/50 ${
                        isSelected ? 'bg-muted' : ''
                      }`}
                      onClick={() => {
                        if (batchMode) {
                          setSelectedCodes(prev => {
                            const next = new Set(prev)
                            if (isSelected) {
                              next.delete(item.code)
                            } else {
                              next.add(item.code)
                            }
                            return next
                          })
                        } else if (itemHasData) {
                          handleSelectStock(item.code)
                        }
                      }}
                    >
                      {batchMode ? (
                        <div className="flex items-center justify-between">
                          <span className="font-medium">{item.code} - {item.name || '-'}</span>
                          <Checkbox
                            checked={isSelected}
                            onCheckedChange={(checked) => {
                              setSelectedCodes(prev => {
                                const next = new Set(prev)
                                if (checked) {
                                  next.add(item.code)
                                } else {
                                  next.delete(item.code)
                                }
                                return next
                              })
                            }}
                          />
                        </div>
                      ) : (
                        <>
                          <div className="flex items-start justify-between mb-2">
                            <div className="flex-1">
                              <div className="flex items-center gap-2">
                                <span className="font-bold text-lg">{item.code}</span>
                                {itemHasData && analysis && badge && (
                                  <Badge className={`${badge.color} text-xs`} variant="outline">
                                    {badge.icon} {analysis.operation_advice}
                                  </Badge>
                                )}
                              </div>
                              <div className="text-sm text-muted-foreground">{item.name || '-'}</div>
                            </div>
                            <div className="flex items-center gap-2">
                              {itemHasData && (
                                <div className={`text-lg font-bold ${getSentimentColor(analysis?.sentiment_score || 50)}`}>
                                  {analysis?.sentiment_score ?? '-'}
                                </div>
                              )}
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  handleRemove(item.code)
                                }}
                              >
                                <Trash2 className="h-4 w-4" />
                              </Button>
                            </div>
                          </div>

                          {itemHasData && (
                            <div className="flex items-center justify-between text-sm">
                              <span className="text-muted-foreground">📅 {formatDate(item.date || '')}</span>
                              {analysis && getTrendType(analysis.trend_prediction) !== 'neutral' && (
                                <div className="flex items-center gap-1 text-muted-foreground">
                                  {getTrendType(analysis.trend_prediction) === 'up' ? (
                                    <TrendingUp className="h-4 w-4" />
                                  ) : (
                                    <TrendingDown className="h-4 w-4" />
                                  )}
                                  <span className="text-xs">{analysis.confidence_level}置信</span>
                                </div>
                              )}
                            </div>
                          )}

                          {itemHasData && (
                            <div className="text-sm">
                              <span className="text-muted-foreground">最新价：</span>
                              <span className={getPctChgColor(item.pct_chg)}>
                                {formatNumber(item.close_price)} ({formatPercent(item.pct_chg)})
                              </span>
                            </div>
                          )}

                          {!itemHasData && (
                            <div className="text-sm text-muted-foreground">
                              无数据，请先同步数据
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </CardContent>
        </Card>

        {/* 右侧分析详情面板 */}
        <Card className="lg:col-span-2">
          {selectedCode && withData.find(w => w.code === selectedCode) ? (
            <>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">
                    {selectedAnalysis?.name || selectedCode} ({selectedCode})
                  </CardTitle>
                  {selectedAnalysis ? (
                    <div className={`text-2xl font-bold ${getSentimentColor(selectedAnalysis.sentiment_score || 50)}`}>
                      {selectedAnalysis.sentiment_score}分
                    </div>
                  ) : (
                    <Button
                      size="sm"
                      onClick={() => handleAnalyze(selectedCode)}
                      disabled={analyzingCode.has(selectedCode)}
                    >
                      {analyzingCode.has(selectedCode) ? (
                        <>
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          分析中...
                        </>
                      ) : (
                        <>
                          <Brain className="mr-2 h-4 w-4" />
                          开始分析
                        </>
                      )}
                    </Button>
                  )}
                </div>
              </CardHeader>
              <CardContent className="max-h-[600px] overflow-y-auto">
                {selectedAnalysis ? (
                  <Tabs defaultValue="dashboard" className="w-full">
                    <TabsList className="grid w-full grid-cols-4">
                      <TabsTrigger value="dashboard">决策</TabsTrigger>
                      <TabsTrigger value="technical">技术</TabsTrigger>
                      <TabsTrigger value="news">情报</TabsTrigger>
                      <TabsTrigger value="raw">原始</TabsTrigger>
                    </TabsList>

                    <TabsContent value="dashboard" className="space-y-4 mt-4">
                      {/* 核心结论 */}
                      {selectedAnalysis.dashboard?.core_conclusion && (
                        <div className="bg-muted/50 p-4 rounded-lg space-y-3">
                          <div>
                            <h3 className="text-sm font-medium text-muted-foreground">核心结论</h3>
                            <p className="text-lg font-medium">{selectedAnalysis.dashboard.core_conclusion.one_sentence}</p>
                          </div>
                          <div className="flex gap-4 text-sm">
                            <div>
                              <span className="text-muted-foreground">信号类型：</span>
                              <Badge className="ml-1">{selectedAnalysis.dashboard.core_conclusion.signal_type}</Badge>
                            </div>
                            <div>
                              <span className="text-muted-foreground">时间敏感度：</span>
                              <span className="ml-1">{selectedAnalysis.dashboard.core_conclusion.time_sensitivity}</span>
                            </div>
                          </div>
                          <div className="space-y-2 text-sm">
                            <div>
                              <span className="text-muted-foreground">空仓者：</span>
                              <span className="ml-1">{selectedAnalysis.dashboard.core_conclusion.position_advice.no_position}</span>
                            </div>
                            <div>
                              <span className="text-muted-foreground">持仓者：</span>
                              <span className="ml-1">{selectedAnalysis.dashboard.core_conclusion.position_advice.has_position}</span>
                            </div>
                          </div>
                        </div>
                      )}

                      {/* 狙位点 */}
                      {selectedAnalysis.dashboard?.battle_plan?.sniper_points && (
                        <div className="bg-muted/50 p-4 rounded-lg space-y-2">
                          <h3 className="text-sm font-medium">狙击点位</h3>
                          <div className="grid grid-cols-2 gap-2 text-sm">
                            <div>💰 {selectedAnalysis.dashboard.battle_plan.sniper_points.ideal_buy}</div>
                            <div>📊 {selectedAnalysis.dashboard.battle_plan.sniper_points.secondary_buy}</div>
                            <div>🛑 {selectedAnalysis.dashboard.battle_plan.sniper_points.stop_loss}</div>
                            <div>🎯 {selectedAnalysis.dashboard.battle_plan.sniper_points.take_profit}</div>
                          </div>
                        </div>
                      )}

                      {/* 检查清单 */}
                      {selectedAnalysis.dashboard?.battle_plan?.action_checklist && (
                        <div className="bg-muted/50 p-4 rounded-lg">
                          <h3 className="text-sm font-medium mb-2">检查清单</h3>
                          <ul className="space-y-1">
                            {selectedAnalysis.dashboard.battle_plan.action_checklist.map((item, i) => (
                              <li key={i} className="text-sm">{item}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </TabsContent>

                    <TabsContent value="technical" className="space-y-4 mt-4">
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">技术面分析</h3>
                        <p className="text-sm">{selectedAnalysis.technical_analysis || '-'}</p>
                      </div>
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">均线系统</h3>
                        <p className="text-sm">{selectedAnalysis.ma_analysis || '-'}</p>
                      </div>
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">量能分析</h3>
                        <p className="text-sm">{selectedAnalysis.volume_analysis || '-'}</p>
                      </div>
                    </TabsContent>

                    <TabsContent value="news" className="space-y-4 mt-4">
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">新闻摘要</h3>
                        <p className="text-sm">{selectedAnalysis.news_summary || '暂无相关新闻'}</p>
                      </div>
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">风险警报</h3>
                        <ul className="space-y-1">
                          {selectedAnalysis.dashboard?.intelligence?.risk_alerts?.map((alert, i) => (
                            <li key={i} className="text-sm text-destructive">🚨 {alert}</li>
                          )) || (
                            <li className="text-sm text-muted-foreground">暂无风险警报</li>
                          )}
                        </ul>
                      </div>
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">利好催化</h3>
                        <ul className="space-y-1">
                          {selectedAnalysis.dashboard?.intelligence?.positive_catalysts?.map((catalyst, i) => (
                            <li key={i} className="text-sm text-green-600">✅ {catalyst}</li>
                          )) || (
                            <li className="text-sm text-muted-foreground">暂无利好催化</li>
                          )}
                        </ul>
                      </div>
                    </TabsContent>

                    <TabsContent value="raw" className="space-y-4 mt-4">
                      <div className="bg-muted/50 p-4 rounded-lg space-y-2">
                        <div>
                          <span className="text-sm text-muted-foreground">趋势预测：</span>
                          <span className="ml-2">{selectedAnalysis.trend_prediction}</span>
                        </div>
                        <div>
                          <span className="text-sm text-muted-foreground">操作建议：</span>
                          <span className="ml-2">{selectedAnalysis.operation_advice}</span>
                        </div>
                        <div>
                          <span className="text-sm text-muted-foreground">置信度：</span>
                          <span className="ml-2">{selectedAnalysis.confidence_level}</span>
                        </div>
                        <div>
                          <span className="text-sm text-muted-foreground">分析时间：</span>
                          <span className="ml-2 text-xs">{formatDateTime(new Date())}</span>
                        </div>
                      </div>
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">综合摘要</h3>
                        <p className="text-sm">{selectedAnalysis.analysis_summary}</p>
                      </div>
                      <div className="bg-muted/50 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2">核心看点</h3>
                        <p className="text-sm">{selectedAnalysis.key_points}</p>
                      </div>
                      <div className="bg-destructive/10 border border-destructive/20 p-4 rounded-lg">
                        <h3 className="text-sm font-medium mb-2 text-destructive">风险提示</h3>
                        <p className="text-sm">{selectedAnalysis.risk_warning}</p>
                      </div>
                    </TabsContent>
                  </Tabs>
                ) : (
                  <div className="flex flex-col items-center justify-center py-12 text-center">
                    <Brain className="h-12 w-12 text-muted-foreground mb-4" />
                    <p className="text-muted-foreground">选择左侧股票查看分析详情</p>
                    <Button
                      onClick={() => handleAnalyze(selectedCode)}
                      disabled={analyzingCode.has(selectedCode)}
                      className="mt-4"
                    >
                      {analyzingCode.has(selectedCode) ? (
                        <>
                          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                          分析中...
                        </>
                      ) : (
                        <>
                          <Brain className="mr-2 h-4 w-4" />
                          开始分析
                        </>
                      )}
                    </Button>
                  </div>
                )}
              </CardContent>
            </>
          ) : (
            <CardContent className="flex flex-col items-center justify-center py-12 text-center">
              <div className="text-6xl mb-4">📊</div>
              <p className="text-muted-foreground">选择左侧股票查看分析详情</p>
              {withData.length === 0 && (
                <Button onClick={() => router.push('/data')} className="mt-4" variant="outline">
                  前往数据管理同步数据
                </Button>
              )}
            </CardContent>
          )}
        </Card>
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="text-sm text-destructive text-center p-4 bg-destructive/10 rounded-lg">
          {error}
          <Button
            variant="ghost"
            size="sm"
            className="mt-2"
            onClick={() => setError('')}
          >
            关闭
          </Button>
        </div>
      )}
    </div>
  )
}
