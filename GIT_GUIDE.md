# Git 操作手册

## 准备工作

在项目文件夹空白处右键 → "在此处打开 PowerShell"

> 项目文件夹路径：`C:\Users\29822\Documents\my-first-repo`

---

## 三步上传文件

### 第 1 步：收集改动

```
git add .
```

> 把文件夹里所有的修改加入"待拍照队列"

---

### 第 2 步：保存快照

```
git commit -m "写清楚你改了什么"
```

> 拍照存档，备注要写清楚方便以后查找
>
> 例如：
>   git commit -m "修改了扫描速度为100mm/s"
>   git commit -m "新增了数据导出功能"

---

### 第 3 步：上传到 GitHub

```
git push
```

> 把本地快照同步到云端

---

## 你的 GitHub 仓库地址

https://github.com/xf9426334/my-first-repo

## 常用命令速查

| 命令 | 作用 |
|------|------|
| git status | 查看当前有哪些改动 |
| git add . | 收集所有改动 |
| git commit -m "..." | 保存快照 |
| git push | 上传到 GitHub |
| git log --oneline | 查看历史记录 |