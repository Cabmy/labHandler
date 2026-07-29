# 实验报告写作指南（lab_report skill 参考材料）

## 实验过程章节的写作风格（长示例）

每一步先用 1-2 句话解释**思考与抉择**，再给命令/现象/截图占位：

```
首先启动容器，进入受害者容器 10.9.0.5 关闭 SYN Cookie 防护并清除 TCP 缓存。

这里我同时减小了半开连接队列的大小，使得攻击更容易成功。

（此处建议附受害者容器关 SYN Cookie + 改队列大小 + 清缓存命令的截图）

在这个受害者容器中同时开启监控半连接队列：

（此处建议附 ss -tnl / netstat -an 监控输出的截图）

接下来开始攻击，synflood.py 已经完成，进入攻击者容器执行攻击脚本即可：

（此处建议附攻击执行截图）

受害者容器中监控到半连接：

（此处建议附半连接队列被填满的截图）

在可信服务器中尝试 telnet 到受害者失败，说明攻击成功。

（此处建议附 telnet 超时截图）
```

## 环境前置段样例

需要改 docker / 系统配置才能跑通时，单独写一段让助教知道"为什么我改了 yml"：

> 在一切开始之前，因为 docker 容器配置问题，需要修改 docker-compose.yml 让容器能联网下载部件：
> ```yaml
> services:
>   attacker:
>     build:
>       context: ./image_ubuntu_mitnick
>       network: host
> ```
> 网络改成主机网络，宿主机开 VPN 即可正常访问外网。
> 另外还需要开启容器的 privileged 权限。

要点：容器/VM 拓扑（多容器画出 IP / 网络示意）、docker-compose.yml 关键改动、
特殊权限（privileged / cap_add / seccomp）。

## 代码贴文中的写法

代码块前一句说明「这段代码做什么 + 关键参数选择理由」：

```python
# tcp_rst.py
#!/usr/bin/env python3
from scapy.all import *

ip = IP(src="10.9.0.5", dst="10.9.0.6")
tcp = TCP(sport=23, dport=33924, flags="R", seq=1987657081)
pkt = ip/tcp
send(pkt, verbose=0)
print("RST packet sent!")
```

伪造一个从 10.9.0.5 发往 10.9.0.6 的 RST 包，seq 设置为 10.9.0.6 期望的值（即 ACK 值）。

## 实验结果表模板

| Task | 是否成功 | 耗时 | 关键现象 |
|---|---|---|---|
| 1.1 (py SYN flood) | ✓ | ~30s | 队列填满，telnet 超时 |
| 1.2 (c SYN flood) | ✓ | ~5s | 比 py 快 6×，无 py 解释器开销 |
| 1.3 (开 SYN Cookie) | ✗（攻击失败合预期） | - | Cookie 不存半开状态，队列不会填满 |

分析段要点：与预期对比（理论 X 实测 Y，差异原因）、意外现象解释、
防御机制有效性证明（攻击失败本身就是防御有效的证据）。
