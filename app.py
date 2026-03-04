gspread.exceptions.APIError: This app has encountered an error. The original error message is redacted to prevent data leaks. Full error details have been recorded in the logs (if you're on Streamlit Cloud, click on 'Manage app' in the lower right of your app).
Traceback:
File "/mount/src/jaeju-stock/app.py", line 843, in <module>
    menu = get_menu_items(active_only=True)
File "/mount/src/jaeju-stock/app.py", line 510, in get_menu_items
    df = read_df("menu_items")
File "/mount/src/jaeju-stock/app.py", line 120, in read_df
    return read_df_cached(tab)
File "/home/adminuser/venv/lib/python3.13/site-packages/streamlit/runtime/caching/cache_utils.py", line 281, in __call__
    return self._get_or_create_cached_value(args, kwargs, spinner_message)
           ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/home/adminuser/venv/lib/python3.13/site-packages/streamlit/runtime/caching/cache_utils.py", line 326, in _get_or_create_cached_value
    return self._handle_cache_miss(cache, value_key, func_args, func_kwargs)
           ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/home/adminuser/venv/lib/python3.13/site-packages/streamlit/runtime/caching/cache_utils.py", line 385, in _handle_cache_miss
    computed_value = self._info.func(*func_args, **func_kwargs)
File "/mount/src/jaeju-stock/app.py", line 110, in read_df_cached
    values = w.get_all_values()
File "/home/adminuser/venv/lib/python3.13/site-packages/gspread/worksheet.py", line 486, in get_all_values
    return self.get_values(
           ~~~~~~~~~~~~~~~^
        range_name=range_name,
        ^^^^^^^^^^^^^^^^^^^^^^
    ...<6 lines>...
        return_type=return_type,
        ^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
File "/home/adminuser/venv/lib/python3.13/site-packages/gspread/worksheet.py", line 463, in get_values
    return self.get(
           ~~~~~~~~^
        range_name=range_name,
        ^^^^^^^^^^^^^^^^^^^^^^
    ...<6 lines>...
        return_type=return_type,
        ^^^^^^^^^^^^^^^^^^^^^^^^
    )
    ^
File "/home/adminuser/venv/lib/python3.13/site-packages/gspread/worksheet.py", line 958, in get
    response = self.client.values_get(
        self.spreadsheet_id, get_range_name, params=params
    )
File "/home/adminuser/venv/lib/python3.13/site-packages/gspread/http_client.py", line 236, in values_get
    r = self.request("get", url, params=params)
File "/home/adminuser/venv/lib/python3.13/site-packages/gspread/http_client.py", line 128, in request
    raise APIError(response)
