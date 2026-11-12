# dglab-vrc


```powershell
poetry install --no-root
poetry run python src/main.py
```


## 設定

```yaml
A:
  - DGLAB/right_leg
B:
  - DGLAB/left_leg

sps:
  enabled: true
  shallow_channel: A
  vrcfury_source: plug
  include_self: false
  max_amplitude: 100
```

頂層 `A`、`B` 為郊狼強度控制的 Avatar 參數名稱，不會控制 Ovc。

| SPS 設定          | 用途                                                                     |
| ----------------- | ------------------------------------------------------------------------ |
| `enabled`         | 是否啟用 SPS → Ovc                                                       |
| `shallow_channel` | 淺處配件接 A 或 B，另一路為深處                                          |
| `vrcfury_source`  | `plug`：讀自己的 Plug；`socket`：讀自己的 Socket；`auto`：兩者取最大深度 |
| `include_self`    | 是否接受 `PenSelf`，由參數分類決定，不以物件所有權推斷                   |
| `max_amplitude`   | 波形振幅上限 0～100；Ovc 通道主強度仍由 App 設定                         |

