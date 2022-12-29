# cspine-research-unet

fully inspired by <a href="https://github.com/milesial/Pytorch-UNet">U-Net: Semantic segmentation with PyTorch<a>

requirements 설치하기
```
pip install -r requirements.txt
```

- [Training](#training)
- [Weights & Biases](#weights--biases)


## Training
다른 옵션이 있으나 default 값 그대로 사용하는 것을 추천 드립니다.
```
python train.py --epochs 10
```

## Weights & Biases
wandb를 사용하기 위해서는 먼저 login을 한 상태여야 합니다. ([자신의 API key 확인하기](https://app.wandb.ai/authorize))
```
wandb login [user API Key]
```
