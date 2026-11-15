"""Analytical stand-ins for a torch scalar, scorer head and optimizer; no ML imports."""


class Scalar:
    def __init__(self, value, backward=None): self.value=value; self.action=backward
    def __mul__(self,n): return Scalar(self.value*n,lambda: self.action(n))
    def detach(self): return self
    def cpu(self): return self
    def item(self): return self.value
    def all(self): return self
    def backward(self): self.action()

class TinyHead:
    """Analytical binary cross entropy; no torch/model/runtime imports."""
    def __init__(self): self.weight=.2; self.grad=0; self.calls=[]
    def __call__(self, **batch):
        self.calls.append(batch['items'])
        return type('Logits',(),{'shape':(len(batch['items']),2),
                               'float':lambda _:batch['items']})(), None

class Optimizer:
    def __init__(self,model): self.model=model; self.steps=0; self.zeros=0
    def zero_grad(self,**kw): self.model.grad=0; self.zeros+=1
    def step(self): self.model.weight-=.01*self.model.grad; self.steps+=1
