# 中文论文 LaTeX 项目

以《论文大纲.md》和《论文计划.md》的研究主线为基础，参考《Large Lemma Miners: Can LLMs Do Induction Proofs for Hardware?》的章节顺序。main.tex 是编译入口，sections 和 appendices 中的标题构成当前目录；引言、相关工作与动机示例已有初稿和参考文献，其他章节仍主要是占位内容，实验结果尚未填充。

各标题后的不可见空段落仅用于空正文时正常分页；开始撰写相应章节时，可用正文替换它。

在本目录运行 bash build.sh，使用 XeLaTeX 和 BibTeX 生成 build/main.pdf；正文以 \cite 命令引用，条目统一维护在 references.bib。版式参照所给论文的 Letter 纸、双栏、通栏标题、紧凑的编号章节标题和无目录排版；中文使用 Noto CJK，拉丁字母使用接近原稿 Times 风格的 TeX Gyre Termes。空白的作者和摘要区域只用于版式预览，填入正式内容时应替换留白。

正文按“引言—相关工作—预备知识—动机示例—方法—实现与实验设置—结果—讨论—结论”组织，另有算法、复现配置和补充结果三个附录。
