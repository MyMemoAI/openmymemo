"""The default memory taxonomy: 5 groups, 14 topics covering the different sides of a person.

Topic slugs are stable identifiers (they are the file names); titles, descriptions, groups and privacy
are written into each file's YAML frontmatter when the vault is created, and the user may change them.
Keywords cover English and Chinese so memories written in either language get filed. Imports land in
``from-<source>`` topics in the ``imported`` group and are sorted into the regular topics later.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUPS: list[dict] = [
    {"id": "who", "name": "Who I am",
     "topics": ["identity", "history", "values"]},
    {"id": "doing", "name": "What I'm doing",
     "topics": ["work", "projects", "goals", "learning"]},
    {"id": "ways", "name": "How I work",
     "topics": ["how-to-work-with-me", "tools", "interests"]},
    {"id": "world", "name": "My world",
     "topics": ["people", "lifestyle"]},
    {"id": "private", "name": "Private",
     "topics": ["health", "finance"]},
]

DEFAULT_PRIVATE = ["health", "finance"]

# Imports land in a topic of their own, ``from-<source>``, in this group (created on the first import).
IMPORTED_GROUP = "imported"
IMPORTED_GROUP_NAME = "Imported"
IMPORT_SOURCE_NAMES = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini", "text": "text",
                       "markdown": "Markdown", "json": "JSON"}

# Topics whose content goes first in what an AI sees.
CONTEXT_PRIORITY = ["how-to-work-with-me", "identity", "work", "projects", "goals"]

# When a memory matches several topics, these weights break the tie. Sensitive topics win on purpose:
# when in doubt, file a memory somewhere private rather than somewhere every AI can read.
# Tool and taste words show up in statements about other things ("CTO, stack is Vue + Swift"), so they count less.
CLASSIFY_WEIGHT = {"people": 1.5, "health": 1.3, "finance": 1.3, "tools": 0.75, "interests": 0.8}

# Phrases that settle the topic on their own ("担任 CTO" is about work even if it names a tech stack).
STRONG_KEYWORDS = {
    "work": ("担任", "职位", "cto", "ceo", "创始人", "founder", "works as", "job title", "my job"),
    "identity": ("我叫", "叫我", "my name", "call me"),
    "projects": ("正在做", "working on"),
    "people": ("女儿", "儿子", "妻子", "丈夫", "daughter", "son ", "wife", "husband"),
}

# Topics whose memories are time-bound by nature: new memories get a review date.
REVIEW_MONTHS = {"projects": 3, "goals": 6, "learning": 6}


@dataclass(frozen=True)
class TopicDef:
    slug: str
    title: str
    description: str
    keywords: tuple[str, ...]


DEFAULT_TOPICS: list[TopicDef] = [
    TopicDef(
        "identity",
        "Identity",
        "Name, how to address me, age, location, languages, time zone",
        ("名字", "叫我", "称呼", "岁", "出生", "生日", "住在", "来自", "所在地", "城市", "国家", "母语", "时区",
         "my name", "call me", "i am ", "i'm ", "years old", "born", "birthday", "live in", "based in",
         "from ", "native", "speak", "timezone", "time zone", "nationality", "pronoun"),
    ),
    TopicDef(
        "history",
        "Background",
        "Education, career history, major life events",
        ("毕业", "大学", "学位", "本科", "硕士", "博士", "曾经", "以前", "之前在", "履历", "经历",
         "graduated", "university", "college", "degree", "phd", "master", "bachelor", "previously",
         "used to", "formerly", "worked at", "background"),
    ),
    TopicDef(
        "values",
        "Values",
        "Principles, beliefs, what I care about, what I won't accept",
        ("相信", "信念", "原则", "价值观", "在意", "重视", "无法接受", "底线", "宗教", "信仰",
         "believe", "value", "principle", "care about", "matters to me", "religion", "faith", "ethic"),
    ),
    TopicDef(
        "work",
        "Work",
        "Role, company, team, responsibilities, industry",
        ("工作", "公司", "职位", "担任", "负责", "任职", "就职", "团队", "同事", "老板", "创业", "创始人", "CEO", "CTO", "工程师", "产品经理", "行业",
         "客户", "job", "work at", "work as", "works as", "works at", "company", "role", "team", "colleague", "manager", "founder",
         "startup", "engineer", "designer", "industry", "client", "employer", "career"),
    ),
    TopicDef(
        "projects",
        "Projects",
        "Things I'm actively working on",
        ("项目", "正在", "在做", "开发", "上线", "发布", "重构", "迁移", "迁到", "评估", "版本", "仓库", "产品",
         "project", "working on", "building", "launch", "release", "migrat", "refactor", "repo", "prototype",
         "evaluating", "considering"),
    ),
    TopicDef(
        "goals",
        "Goals",
        "Short- and long-term goals and plans",
        ("目标", "计划", "打算", "希望", "想要", "梦想", "今年", "明年", "要在",
         "goal", "plan to", "planning", "want to", "hope to", "aim", "aspire", "this year", "next year", "dream"),
    ),
    TopicDef(
        "learning",
        "Learning",
        "What I'm learning or want to learn, and how I learn best",
        ("学习", "在学", "想学", "课程", "考试", "备考", "练习", "读书笔记",
         "日语", "英语", "法语", "德语", "韩语", "西班牙语",
         "learning", "studying", "course", "exam", "practice", "tutorial", "learn ",
         "japanese", "spanish", "french", "german", "korean", "mandarin"),
    ),
    TopicDef(
        "how-to-work-with-me",
        "How to work with me",
        "Answer style, language, format, things to avoid — instructions for AI",
        ("回答", "回复", "请用", "用中文", "用英文", "不要", "别", "简洁", "详细", "格式", "语气", "先给结论", "代码风格",
         "respond", "reply", "answer", "always", "never", "please", "concise", "verbose", "format", "tone",
         "explain", "when you", "don't", "do not", "avoid", "bullet", "markdown"),
    ),
    TopicDef(
        "tools",
        "Tools and setup",
        "Devices, OS, editors, tech stack, everyday software",
        ("电脑", "手机", "系统", "编辑器", "技术栈", "框架", "语言", "软件", "工具", "用的是",
         "mac", "macbook", "主力机", "vs code", "macos", "windows", "linux", "iphone", "android", "vscode", "vim", "cursor", "python", "typescript",
         "javascript", "react", "vue", "swift", "rust", "go ", "stack", "framework", "editor", "ide", "uses ", "using "),
    ),
    TopicDef(
        "interests",
        "Interests and taste",
        "Books, film, music, food, sports, hobbies, taste",
        ("喜欢", "爱好", "兴趣", "电影", "音乐", "书", "小说", "游戏", "运动", "跑步", "徒步", "爬山", "游泳",
         "瑜伽", "旅行", "摄影", "美食", "咖啡",
         "likes", "enjoy", "love ", "hobby", "interest", "movie", "film", "music", "book", "novel", "game",
         "sport", "running", "hiking", "climbing", "swimming", "yoga", "travel", "photography", "food", "coffee", "favorite", "fan of"),
    ),
    TopicDef(
        "people",
        "People",
        "Family, friends, colleagues, pets, and how I relate to them",
        ("妻子", "丈夫", "老婆", "老公", "女朋友", "男朋友", "伴侣", "孩子", "儿子", "女儿", "父母", "爸爸", "妈妈",
         "朋友", "家人", "猫", "狗", "宠物",
         "wife", "husband", "partner", "girlfriend", "boyfriend", "kid", "child", "son", "daughter", "parent",
         "mother", "father", "mom", "dad", "friend", "family", "cat", "dog", "pet", "sibling", "brother", "sister"),
    ),
    TopicDef(
        "lifestyle",
        "Lifestyle",
        "Routine, home, eating habits, getting around, daily schedule",
        ("作息", "早起", "晚睡", "起床", "早上", "每天", "睡觉", "通勤", "开车", "住", "房子", "公寓", "做饭", "吃素", "素食", "周末",
         "routine", "wake up", "sleep", "commute", "drive", "apartment", "house", "cook", "vegetarian",
         "vegan", "weekend", "schedule", "habit", "every morning", "every day", "daily"),
    ),
    TopicDef(
        "health",
        "Health",
        "Medical history, medication, dietary restrictions, fitness",
        ("健康", "病", "医生", "医院", "药", "过敏", "体检", "血压", "怀孕", "抑郁", "焦虑", "受伤", "手术", "体重",
         "health", "doctor", "hospital", "medication", "medicine", "allerg", "diagnos", "pregnan", "depress",
         "anxiety", "injur", "surgery", "weight", "diet", "therapy", "condition"),
    ),
    TopicDef(
        "finance",
        "Finance",
        "Income, assets, budget, insurance",
        ("工资", "收入", "薪水", "存款", "资产", "投资", "股票", "基金", "房贷", "贷款", "预算", "保险", "税",
         "salary", "income", "savings", "asset", "invest", "stock", "fund", "mortgage", "loan", "budget",
         "insurance", "tax", "debt", "net worth"),
    ),
]

TOPIC_BY_SLUG = {t.slug: t for t in DEFAULT_TOPICS}


def default_group_of(slug: str) -> str | None:
    return next((g["id"] for g in GROUPS if slug in g["topics"]), None)


def import_topic_slug(source: str) -> str:
    """``from-chatgpt``, ``from-claude``, …: the topic an import from ``source`` lands in."""
    return f"from-{source}"


def default_topics_config() -> dict:
    """``.openmymemo/topics.yaml``: group names and sidebar order. Privacy lives in each file's frontmatter."""
    return {
        "format": 2,
        "groups": [{"id": g["id"], "name": g["name"], "topics": list(g["topics"])} for g in GROUPS],
    }
