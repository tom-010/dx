document = Documents.get()
page1 = documents.pages[0]
page2 = documents.pages[0]

word_count = WordCount(
    count=(len(page1.content) + len(page2.content))
)
word_count.save(
    producer="page-count-adder",
    sources=[
        page1.content, 
        page2.content
    ]
)
