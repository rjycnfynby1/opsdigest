def test_smoke():
    import opsdigest
    import opsdigest.render
    
    assert isinstance(opsdigest.__version__, str)
    assert len(opsdigest.__version__) > 0
    
    assert "reply" in opsdigest.render.TYPE_RU
    assert opsdigest.render.TYPE_RU["reply"] == "Ответить"
