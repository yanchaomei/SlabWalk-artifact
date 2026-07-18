#include "lavd/pq.hh"
#include <cstdio>
#include <random>
#include <algorithm>
#include <set>
int main(){
  const u32 dim=128, m=16; const size_t N=4000;
  std::mt19937 rng(7);
  std::normal_distribution<float> noise(0.f,0.3f);
  std::uniform_real_distribution<float> u(-1.f,1.f);
  const int C=40; std::vector<std::vector<float>> ctr(C, std::vector<float>(dim));
  for(auto&c:ctr) for(auto&x:c) x=u(rng);
  std::vector<float> data(N*dim);
  for(size_t i=0;i<N;i++){ int c=(int)(rng()%C); for(u32 d=0;d<dim;d++) data[i*dim+d]=ctr[c][d]+noise(rng); }
  lavd::PQ pq; pq.fit(dim,m,data.data(),N);
  std::vector<u8> c1(m),c2(m); pq.encode(data.data(),c1.data()); pq.encode(data.data(),c2.data());
  bool det=(c1==c2);
  std::vector<byte_t> buf(pq.params_bytes()); pq.write_params(buf.data());
  lavd::PQ pq2; pq2.read_params(buf.data());
  std::vector<u8> c3(m); pq2.encode(data.data(),c3.data()); bool rt=(c1==c3);
  std::vector<u8> codes(N*m); for(size_t i=0;i<N;i++) pq.encode(data.data()+i*dim,codes.data()+i*m);
  int nq=50; double hit=0; std::vector<f32> lut(pq.lut_size());
  for(int q=0;q<nq;q++){
    const float* qv=data.data()+(rng()%N)*dim;
    std::vector<std::pair<float,int>> tl(N);
    for(size_t i=0;i<N;i++){ float a=0; for(u32 d=0;d<dim;d++){float t=qv[d]-data[i*dim+d];a+=t*t;} tl[i]={a,(int)i}; }
    std::partial_sort(tl.begin(),tl.begin()+10,tl.end());
    pq.build_lut(qv,lut.data());
    std::vector<std::pair<float,int>> al(N);
    for(size_t i=0;i<N;i++) al[i]={(float)pq.adc(lut.data(),codes.data()+i*m),(int)i};
    std::partial_sort(al.begin(),al.begin()+100,al.end());
    std::set<int> top100; for(int k=0;k<100;k++) top100.insert(al[k].second);
    int h=0; for(int k=0;k<10;k++) if(top100.count(tl[k].second))h++;
    hit+=h/10.0;
  }
  printf("determinism=%d roundtrip=%d recall@10-in-top100=%.3f code_bytes=%zu (scalar int8=%u, %.1fx smaller)\n",
         det,rt,hit/nq,pq.code_bytes(),dim,(double)dim/pq.code_bytes());
  return 0;
}
