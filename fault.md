TX-Clerk$ echo "from .reward_function import GRPORewardFunction" > audiochat/grpo/__init__.py
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ # 修改导入语句
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ sed -i "s/from.*SimpleRewardFn.*/from audiochat.grpo import GRPORewardFunction/" train_grpo.py
# 修改奖励函数初始化
sed -i "s/self.reward_fn = SimpleRewardFn(self.tokenizer)/self.reward_fn = GRPORewardF(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ # 修改奖励函数初始化
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ sed -i "s/self.reward_fn = SimpleRewardFn(self.tokenizer)/self.reward_fn = GRPORewardFunction()/" train_grpo.py
"s/reward = self.reward_fn.score(prompts, responses)/reward = self.reward_fn.compute_reward(responses[0])(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ # 修改 score 方法调用
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ sed -i "s/reward = self.reward_fn.score(prompts, responses)/reward = self.reward_fn.compute_reward(responses[0])/" train_grpo.py
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ CUDA_VISIBLE_DEVICES=1 torchrun --nproc_per_node=1 train_grpo.py \
>   --model_name_or_path saves/sft_qwen3.5 \
>   --train_data data/grpo_train_data.jsonl \
>   --output_dir saves/grpo_qwen3.5 \
>   --num_train_epochs 2 \
>   --per_device_train_batch_size 1 \
>   --gradient_accumulation_steps 4 \
>   --learning_rate 1e-5 \
>   --logging_steps 10
✅ 训练设备：cuda:0 | GPU: NVIDIA A100-PCIE-40GB
============================================================
GRPO 强化学习训练
============================================================
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/tokenizer_config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 4544cf42-3c99-48fc-b1fc-f1b29b8fe048)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/tokenizer_config.json
Retrying in 1s [Retry 1/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/tokenizer_config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: a1f1025e-b7e1-4091-8309-55d77513ae85)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/tokenizer_config.json
Retrying in 2s [Retry 2/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/tokenizer_config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 9ac79776-29b6-4b5f-8ec5-f769c4911659)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/tokenizer_config.json
Retrying in 4s [Retry 3/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/tokenizer_config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 0bbfdf16-b631-472a-a163-eab971428173)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/tokenizer_config.json
Retrying in 8s [Retry 4/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/tokenizer_config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 0cea3bba-6b3c-460e-8181-c9deb6444d87)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/tokenizer_config.json
Retrying in 8s [Retry 5/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/tokenizer_config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: e08ff92e-836d-40ed-9653-7d5d87c21d11)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/tokenizer_config.json
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 1996542e-2a55-452f-b7ff-1aec67f15498)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/config.json
Retrying in 1s [Retry 1/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: af499c96-2450-4667-8434-4be6a968589c)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/config.json
Retrying in 2s [Retry 2/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 90fd28f4-6e00-4f20-9d7f-869e9f402781)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/config.json
Retrying in 4s [Retry 3/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: e74578f6-8543-4f5a-a476-941e5af23645)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/config.json
Retrying in 8s [Retry 4/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 21307fdd-c9f4-46dc-99e3-aa7064f111db)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/config.json
Retrying in 8s [Retry 5/5].
'(MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 8e9c8b4e-20e9-4391-b759-14c653c33739)')' thrown while requesting HEAD https://huggingface.co/saves/sft_qwen3.5/resolve/main/config.json
Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connection.py", line 204, in _new_conn
    sock = connection.create_connection(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/util/connection.py", line 85, in create_connection
    raise err
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/util/connection.py", line 73, in create_connection
    sock.connect(sa)
ConnectionRefusedError: [Errno 111] Connection refused

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connectionpool.py", line 787, in urlopen
    response = self._make_request(
               ^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connectionpool.py", line 488, in _make_request
    raise new_e
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connectionpool.py", line 464, in _make_request
    self._validate_conn(conn)
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connectionpool.py", line 1093, in _validate_conn
    conn.connect()
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connection.py", line 759, in connect
    self.sock = sock = self._new_conn()
                       ^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connection.py", line 219, in _new_conn
    raise NewConnectionError(
urllib3.exceptions.NewConnectionError: HTTPSConnection(host='huggingface.co', port=443): Failed to establish a new connection: [Errno 111] Connection refused

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/requests/adapters.py", line 644, in send
    resp = conn.urlopen(
           ^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/connectionpool.py", line 841, in urlopen
    retries = retries.increment(
              ^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/urllib3/util/retry.py", line 535, in increment
    raise MaxRetryError(_pool, url, reason) from reason  # type: ignore[arg-type]
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
urllib3.exceptions.MaxRetryError: HTTPSConnectionPool(host='huggingface.co', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host='huggingface.co', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 1550, in _get_metadata_or_catch_error
    metadata = get_hf_file_metadata(
               ^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/utils/_validators.py", line 114, in _inner_fn
    return fn(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 1467, in get_hf_file_metadata
    r = _request_wrapper(
        ^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 283, in _request_wrapper
    response = _request_wrapper(
               ^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 306, in _request_wrapper
    response = http_backoff(method=method, url=url, **params)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/utils/_http.py", line 326, in http_backoff
    raise err
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/utils/_http.py", line 307, in http_backoff
    response = session.request(method=method, url=url, **kwargs)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/requests/sessions.py", line 589, in request
    resp = self.send(prep, **send_kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/requests/sessions.py", line 703, in send
    r = adapter.send(request, **kwargs)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/utils/_http.py", line 96, in send
    return super().send(request, *args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/requests/adapters.py", line 677, in send
    raise ConnectionError(e, request=request)
requests.exceptions.ConnectionError: (MaxRetryError('HTTPSConnectionPool(host=\'huggingface.co\', port=443): Max retries exceeded with url: /saves/sft_qwen3.5/resolve/main/config.json (Caused by NewConnectionError("HTTPSConnection(host=\'huggingface.co\', port=443): Failed to establish a new connection: [Errno 111] Connection refused"))'), '(Request ID: 8e9c8b4e-20e9-4391-b759-14c653c33739)')

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/transformers/utils/hub.py", line 402, in cached_file
    resolved_file = hf_hub_download(
                    ^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/utils/_validators.py", line 114, in _inner_fn
    return fn(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 1014, in hf_hub_download
    return _hf_hub_download_to_cache_dir(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 1121, in _hf_hub_download_to_cache_dir
    _raise_on_head_call_error(head_call_error, force_download, local_files_only)
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/huggingface_hub/file_download.py", line 1665, in _raise_on_head_call_error
    raise LocalEntryNotFoundError(
huggingface_hub.errors.LocalEntryNotFoundError: An error happened while trying to locate the file on the Hub and we cannot find the requested files in the local cache. Please check your connection and try again or make sure your Internet connection is on.

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/train_grpo.py", line 276, in <module>
    main()
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/train_grpo.py", line 272, in main
    trainer = GRPOTrainer(train_args)
              ^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/train_grpo.py", line 70, in __init__
    self.model, self.tokenizer, self.optimizer = self.load_model_and_tokenizer()
                                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/train_grpo.py", line 76, in load_model_and_tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/transformers/models/auto/tokenization_auto.py", line 854, in from_pretrained
    config = AutoConfig.from_pretrained(
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/transformers/models/auto/configuration_auto.py", line 976, in from_pretrained
    config_dict, unused_kwargs = PretrainedConfig.get_config_dict(pretrained_model_name_or_path, **kwargs)      
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^      
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/transformers/configuration_utils.py", line 632, in get_config_dict
    config_dict, kwargs = cls._get_config_dict(pretrained_model_name_or_path, **kwargs)
                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/transformers/configuration_utils.py", line 689, in _get_config_dict
    resolved_config_file = cached_file(
                           ^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/transformers/utils/hub.py", line 445, in cached_file
    raise EnvironmentError(
OSError: We couldn't connect to 'https://huggingface.co' to load this file, couldn't find it in the cached files and it looks like saves/sft_qwen3.5 is not the path to a directory containing a file named config.json.        
Checkout your internet connection or see how to run the library in offline mode at 'https://huggingface.co/docs/transformers/installation#offline-mode'.
[2026-05-05 19:39:05,554] torch.distributed.elastic.multiprocessing.api: [ERROR] failed (exitcode: 1) local_rank: 0 (pid: 1719051) of binary: /data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/bin/python3
Traceback (most recent call last):
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/bin/torchrun", line 10, in <module>
    sys.exit(main())
             ^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/torch/distributed/elastic/multiprocessing/errors/__init__.py", line 347, in wrapper
    return f(*args, **kwargs)
           ^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/torch/distributed/run.py", line 812, in main
    run(args)
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/torch/distributed/run.py", line 803, in run
    elastic_launch(
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/torch/distributed/launcher/api.py", line 135, in __call__
    return launch_agent(self._config, self._entrypoint, list(args))
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/data/wbt333/DiTX-Clerk/DiTX-Clerk/uv_env/lib/python3.12/site-packages/torch/distributed/launcher/api.py", line 268, in launch_agent
    raise ChildFailedError(
torch.distributed.elastic.multiprocessing.errors.ChildFailedError:
============================================================
train_grpo.py FAILED
------------------------------------------------------------
Failures:
  <NO_OTHER_FAILURES>
------------------------------------------------------------
Root Cause (first observed failure):
[0]:
  time      : 2026-05-05_19:39:05
  host      : vpdlab-X640-G40
  rank      : 0 (local_rank: 0)
  exitcode  : 1 (pid: 1719051)
  error_file: <N/A>
  traceback : To enable traceback see: https://pytorch.org/docs/stable/elastic/errors.html
============================================================
(uv_env) (base) wbt333@vpdlab-X640-G40:/data/wbt333/DiTX-Clerk/DiTX-Clerk$ 