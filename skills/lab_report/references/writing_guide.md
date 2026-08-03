# Lab Report Writing Guide (lab_report skill reference material)

## Writing style for the experiment-process section (long example)

Before each step, first use 1-2 sentences to explain the **thinking and decisions**, then give the command / phenomenon / screenshot placeholder:

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

## Environment-prerequisite section sample

When docker / system config must be changed to make it run, write a separate paragraph so the TA knows "why I changed the yml":

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

Key points: container/VM topology (for multi-container draw out IPs / network diagram), key docker-compose.yml changes,
special permissions (privileged / cap_add / seccomp).

## How to paste code inline

One sentence before the code block stating "what this code does + rationale for key parameter choices":

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

Forge an RST packet sent from 10.9.0.5 to 10.9.0.6, with seq set to the value 10.9.0.6 expects (i.e. the ACK value).

## Experiment result table template

| Task | 是否成功 | 耗时 | 关键现象 |
|---|---|---|---|
| 1.1 (py SYN flood) | ✓ | ~30s | 队列填满，telnet 超时 |
| 1.2 (c SYN flood) | ✓ | ~5s | 比 py 快 6×，无 py 解释器开销 |
| 1.3 (开 SYN Cookie) | ✗（攻击失败合预期） | - | Cookie 不存半开状态，队列不会填满 |

Key points for the analysis section: comparison with expectations (theory X vs measured Y, reason for the difference), explanation of unexpected phenomena,
proof of defense mechanism effectiveness (the attack failing is itself evidence the defense works).
