# Monica Medical AI Server

## 本地开发

```bash
chmod +x dev-start.sh
./dev-start.sh           # 启动
./dev-start.sh stop      # 停止
./dev-start.sh restart   # 重启
```

首次运行会自动创建 `.venv`、安装依赖、生成 `.env.local`。

---

## 服务器部署

```bash
sudo ./deploy.sh install   # 首次安装
sudo ./deploy.sh stop      # 停止
sudo ./deploy.sh restart   # 重启
sudo ./deploy.sh update    # 更新代码并重启
```
