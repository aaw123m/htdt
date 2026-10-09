// Independent reference Gaussian functional exporter; diagnostic, not production.
#include "mfem.hpp"
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
namespace {
class SpatialGaussian final : public mfem::Coefficient {
 public:
  SpatialGaussian(double x,double y,double z,double sigma)
    : center_{x,y,z}, sigma_(sigma) {}
  double Eval(mfem::ElementTransformation &T,const mfem::IntegrationPoint &ip) override {
    mfem::Vector xyz(3);
    T.Transform(ip,xyz);
    double r2=0;
    for(int i=0;i<3;++i) r2+=(xyz[i]-center_[i])*(xyz[i]-center_[i]);
    return std::exp(-0.5*r2/(sigma_*sigma_));
  }
 private:
  double center_[3],sigma_;
};
mfem::Mesh SlopedRoom() {
  mfem::Mesh mesh(3,8,6,0,3);
  const double vertices[8][3]={
    {0,0,0},{4,0,0},{4,4,0},{0,4,0},
    {0,0,4},{4,0,4},{4,4,3},{0,4,3}};
  const int tets[6][4]={{0,1,2,6},{0,2,3,6},{0,3,7,6},
                        {0,7,4,6},{0,4,5,6},{0,5,1,6}};
  for(const auto &v:vertices) mesh.AddVertex(v);
  for(const auto &t:tets) mesh.AddTet(t,1);
  mesh.FinalizeTetMesh(1,0,true);
  return mesh;
}
std::vector<double> AssembleGaussian(mfem::FiniteElementSpace &fes,
                 const double point[3],double sigma) {
  SpatialGaussian gaussian(point[0],point[1],point[2],sigma);
  mfem::LinearForm lf(&fes);
  auto *integration=new mfem::DomainLFIntegrator(gaussian);
  integration->SetIntRule(&mfem::IntRules.Get(mfem::Geometry::TETRAHEDRON,10));
  lf.AddDomainIntegrator(integration);
  lf.Assemble();
  double integral=0;
  for(int i=0;i<lf.Size();++i) integral+=lf[i];
  if(!(std::isfinite(integral)&&integral>0))
    throw std::runtime_error("nonpositive or nonfinite Gaussian normalization");
  std::vector<double> result(lf.Size());
  double normalized_sum=0;
  for(int i=0;i<lf.Size();++i) {
    result[i]=lf[i]/integral;
    if(!std::isfinite(result[i])) throw std::runtime_error("nonfinite functional");
    normalized_sum+=result[i];
  }
  if(std::abs(normalized_sum-1.0)>1e-10)
    throw std::runtime_error("Gaussian P2 partition of unity violated");
  return result;
}
void VectorJSON(std::ostream &out,const std::vector<double> &v) {
  out<<"[";
  for(size_t i=0;i<v.size();++i) { if(i) out<<","; out<<v[i]; }
  out<<"]";
}
int Main(int argc,char **argv) {
  int refinement=-1;
  std::string output;
  for(int i=1;i<argc;++i) {
    std::string s=argv[i];
    if(s=="--refinement"&&i+1<argc) refinement=std::stoi(argv[++i]);
    else if(s=="--output"&&i+1<argc) output=argv[++i];
    else throw std::runtime_error("unknown or missing argument");
  }
  if(refinement<2||refinement>4||output.empty())
    throw std::runtime_error("only preregistered r2-4 and output allowed");
  auto mesh=SlopedRoom();
  for(int j=0;j<refinement;++j) mesh.UniformRefinement();
  if(mesh.GetNE()!=6*static_cast<int>(std::pow(8.0,refinement)))
    throw std::runtime_error("P2 tetra geometry changed");
  mfem::H1_FECollection fec(2,3);
  mfem::FiniteElementSpace fes(&mesh,&fec);
  const int expected[]={0,0,729,4913,35937};
  if(fes.GetVSize()!=expected[refinement])
    throw std::runtime_error("P2 numbering or spatial dimension changed");
  const double source[3]={1.5,2,2},receiver[3]={2.5,2,2};
  std::ofstream out(output,std::ios::binary);
  if(!out) throw std::runtime_error("cannot open output");
  out<<std::setprecision(17);
  out<<"{\n\"schema_version\":\"htdt.r130d.independent-mfem-spatial-gaussian-functionals-1\",";
  out<<"\n\"order\":2,\n\"refinement\":"<<refinement<<",\n\"dofs\":"<<fes.GetVSize()<<",";
  out<<"\n\"elements\":"<<mesh.GetNE()<<",\n\"quadrature_order\":10,";
  const double sigmas[2]={0.35,0.70};
  out<<"\n\"cases\":[";
  for(int i=0;i<2;++i) {
    auto b=AssembleGaussian(fes,source,sigmas[i]);
    auto r=AssembleGaussian(fes,receiver,sigmas[i]);
    if(i) out<<",";
    out<<"\n{\"sigma_m\":"<<sigmas[i]<<",\"source_functional\":";
    VectorJSON(out,b);
    out<<",\"receiver_functional\":";
    VectorJSON(out,r);
    out<<"}";
  }
  out<<"]\n}\n";
  if(!out) throw std::runtime_error("cannot write Gaussian functional file");
  return 0;
}
}
int main(int argc,char **argv) {
  try {return Main(argc,argv);}
  catch(const std::exception &e) {
    std::cerr<<"MFEM Gaussian exporter failed: "<<e.what()<<"\n";
    return 2;
  }
}
